# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Infrahub GraphQL inventory provider."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, quote, urljoin

import httpx

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    InventoryProvider,
    InventoryProviderError,
    PlatformServices,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderTestResult,
    ResourceRef,
    ResourceType,
    SiteRef,
)

from .config import DEFAULT_FIELD_MAP, InfrahubProviderConfig

KNOWN_AUTH_SCHEMES = frozenset({"bearer", "token", "basic"})


def _lookup_path(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(value, list):
            collected = []
            for item in value:
                if isinstance(item, Mapping):
                    collected.append(item.get(part))
            value = collected
        else:
            return None
    return value


def _build_selection_tree(paths: builtins.list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for path in paths:
        parts = [part.strip() for part in path.split(".") if part.strip()]
        if not parts:
            continue
        current = tree
        for part in parts:
            current = current.setdefault(part, {})
    return tree


def _render_selection_tree(tree: Mapping[str, Any]) -> str:
    selections: builtins.list[str] = []
    for field_name, child in tree.items():
        if isinstance(child, Mapping) and child:
            selections.append(f"{field_name} {{ {_render_selection_tree(child)} }}")
        else:
            selections.append(field_name)
    return " ".join(selections)


def _assign_nested(target: dict[str, Any], path: str, value: Any) -> None:
    parts = [part.strip() for part in path.split(".") if part.strip()]
    if not parts:
        return

    current = target
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _safe_graphql_error_details(errors: Any) -> dict[str, Any]:
    if not isinstance(errors, list):
        return {}

    safe_errors: builtins.list[dict[str, Any]] = []
    for error in errors[:10]:
        if not isinstance(error, Mapping):
            continue

        safe_error: dict[str, Any] = {}
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            safe_error["message"] = message.strip()

        path = error.get("path")
        if isinstance(path, list):
            safe_error["path"] = [str(part) for part in path if part is not None]

        extensions = error.get("extensions")
        if isinstance(extensions, Mapping):
            safe_extensions: dict[str, str] = {}
            for key in ("code", "classification"):
                value = extensions.get(key)
                if value is not None:
                    safe_extensions[key] = str(value)
            if safe_extensions:
                safe_error["extensions"] = safe_extensions

        if safe_error:
            safe_errors.append(safe_error)

    return {"graphql_errors": safe_errors} if safe_errors else {}


class InfrahubInventoryProvider(InventoryProvider):
    provider_type = "infrahub"
    capabilities = frozenset({Capability.READ, Capability.QUERY})
    supported_resources = frozenset({ResourceType.DEVICE, ResourceType.SITE})
    config_schema = InfrahubProviderConfig.model_json_schema()

    def __init__(
        self, *, provider_id: str, config: dict[str, Any], services: PlatformServices
    ) -> None:
        self.id = provider_id
        self.config = InfrahubProviderConfig.model_validate(config or {})
        self._field_map = dict(DEFAULT_FIELD_MAP)
        self._field_map.update(self.config.field_map)
        self._services = services

    async def list(
        self,
        resource_type: ResourceType,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ResourceRef]:
        if resource_type == ResourceType.DEVICE:
            return [
                ResourceRef(
                    provider_id=d.provider_id,
                    resource_type=ResourceType.DEVICE,
                    external_id=d.external_id or d.name,
                    display_name=d.safe_display_name,
                    descriptive=d.descriptive,
                )
                for d in await self.query_devices("", limit=limit, context=context)
            ]
        if resource_type == ResourceType.SITE:
            return [
                ResourceRef(
                    provider_id=s.provider_id,
                    resource_type=ResourceType.SITE,
                    external_id=s.external_id,
                    display_name=s.name,
                    descriptive=s.descriptive,
                )
                for s in await self.list_sites(limit=limit, context=context)
            ]
        return []

    async def get(
        self, resource_type: ResourceType, external_id: str, *, context: ProviderCallContext
    ) -> DeviceDescriptor | SiteRef | ResourceRef:
        if resource_type == ResourceType.DEVICE:
            results = await self.query_devices(
                "", limit=self._services.limits.max_run_targets, context=context
            )
            for descriptor in results:
                if descriptor.external_id == external_id:
                    return descriptor
        elif resource_type == ResourceType.SITE:
            for site in await self.list_sites(
                limit=self._services.limits.max_preview_devices, context=context
            ):
                if site.external_id == external_id:
                    return site
        else:
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "Infrahub resource type is not supported",
                context.operation,
            )
        raise self._error(
            ProviderErrorCode.NOT_FOUND, "Infrahub resource not found", context.operation
        )

    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        result_limit = limit or self._services.limits.max_run_targets
        query = self._build_query(result_limit)
        payload = await self._graphql(query, {}, context)
        nodes = self._extract_nodes(payload)
        descriptors = [
            descriptor
            for node in nodes
            if self._matches_expr((descriptor := self._map_device(node)), expr)
        ][:result_limit]
        self._reject_duplicate_external_ids(descriptors, context.operation)
        return descriptors

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        result_limit = limit or self._services.limits.max_preview_devices
        payload = await self._graphql(self._build_site_query(result_limit), {}, context)
        nodes = self._extract_nodes(payload)
        seen: dict[str, SiteRef] = {}
        site_path = self._site_query_path()
        for node in nodes:
            site_ref = self._site_ref_from_value(_lookup_path(node, site_path))
            if site_ref is not None:
                seen.setdefault(site_ref.external_id, site_ref)
            if len(seen) >= result_limit:
                break
        return list(seen.values())

    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        start = monotonic()
        safe_details: dict[str, Any] = {}
        try:
            await self._services.validate_url(str(self.config.url), operation="test_connection")
            await self._graphql(
                self._build_query(1),
                {},
                context,
            )
            ok = True
            error = None
            message = "Infrahub connection succeeded"
        except InventoryProviderError as exc:
            ok = False
            error = exc.envelope.to_dict()
            message = exc.envelope.message
            safe_details = exc.envelope.safe_details
        return ProviderTestResult(
            ok=ok,
            provider_id=self.id,
            provider_type=self.provider_type,
            latency_ms=int((monotonic() - start) * 1000),
            message=message,
            error=error,
            safe_details=safe_details,
        )

    def _query_selection_paths(self) -> builtins.list[str]:
        seen: set[str] = set()
        selection_paths: builtins.list[str] = []
        for path in ["id", "display_label", *self._field_map.values()]:
            stripped = path.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            selection_paths.append(stripped)
        return selection_paths

    def _build_query(self, limit: int) -> str:
        kind = self.config.device_kind
        selection_set = _render_selection_tree(_build_selection_tree(self._query_selection_paths()))
        return f"""
        query HegemonyInventoryDevices {{
          {kind}(limit: {limit}) {{
            edges {{ node {{ {selection_set} }} }}
          }}
        }}
        """

    def _site_query_path(self) -> str:
        return (self._field_map.get("site") or DEFAULT_FIELD_MAP["site"]).strip()

    def _build_site_query(self, limit: int) -> str:
        kind = self.config.device_kind
        selection_set = _render_selection_tree(_build_selection_tree([self._site_query_path()]))
        return f"""
        query HegemonyInventorySites {{
          {kind}(limit: {limit}) {{
            edges {{ node {{ {selection_set} }} }}
          }}
        }}
        """

    def _graphql_endpoint(self) -> str:
        base_url = str(self.config.url).rstrip("/") + "/"
        branch = self.config.branch.strip()
        if branch and branch != "main":
            return urljoin(base_url, f"graphql/{quote(branch, safe='')}")
        return urljoin(base_url, "graphql")

    def _request_header_candidates(
        self, token: str | None
    ) -> builtins.list[tuple[str | None, str, dict[str, str]]]:
        if token is None:
            return []
        stripped = token.strip()
        if not stripped:
            return []

        def _dedupe(
            candidates: builtins.list[tuple[str | None, str, dict[str, str]]],
        ) -> builtins.list[tuple[str | None, str, dict[str, str]]]:
            seen: set[tuple[tuple[str, str], ...]] = set()
            unique: builtins.list[tuple[str | None, str, dict[str, str]]] = []
            for header_name, label, headers in candidates:
                key = tuple(sorted(headers.items()))
                if key in seen:
                    continue
                seen.add(key)
                unique.append((header_name, label, headers))
            return unique

        prefix, _, remainder = stripped.partition(" ")
        if remainder and prefix.lower() in KNOWN_AUTH_SCHEMES:
            candidates = [
                ("Authorization", f"Authorization: {prefix}", {"Authorization": stripped})
            ]
            if prefix.lower() in {"bearer", "token"}:
                raw_token = remainder.strip()
                if raw_token:
                    candidates.append(
                        ("X-INFRAHUB-KEY", "X-INFRAHUB-KEY", {"X-INFRAHUB-KEY": raw_token})
                    )
            return _dedupe(candidates)

        return _dedupe(
            [
                ("X-INFRAHUB-KEY", "X-INFRAHUB-KEY", {"X-INFRAHUB-KEY": stripped}),
                (
                    "Authorization",
                    "Authorization: Bearer",
                    {"Authorization": f"Bearer {stripped}"},
                ),
                (
                    "Authorization",
                    "Authorization: Token",
                    {"Authorization": f"Token {stripped}"},
                ),
            ]
        )

    async def _graphql(
        self, query: str, variables: dict[str, Any], context: ProviderCallContext
    ) -> dict[str, Any]:
        await self._services.validate_url(str(self.config.url), operation=context.operation)
        endpoint = self._graphql_endpoint()
        endpoint_host = httpx.URL(endpoint).host
        token = await self._services.resolve_secret_ref(
            self.config.token_ref, operation=context.operation
        )
        header_candidates = self._request_header_candidates(token)
        if not header_candidates:
            raise self._error(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "Infrahub API token resolved to an empty value",
                context.operation,
            )
        auth_attempts = [label for _header_name, label, _headers in header_candidates]
        retry_attempts = 4
        for attempt in range(retry_attempts):
            should_retry = False
            try:
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_seconds,
                    verify=self.config.verify_tls,
                    follow_redirects=False,
                ) as client:
                    for auth_index, (auth_header_name, auth_label, headers) in enumerate(
                        header_candidates
                    ):
                        resp = await client.post(
                            endpoint,
                            json={"query": query, "variables": variables},
                            headers=headers,
                        )
                        if (
                            resp.status_code in {401, 403}
                            and auth_index < len(header_candidates) - 1
                        ):
                            continue
                        if resp.status_code in {401, 403}:
                            raise self._error(
                                ProviderErrorCode.AUTH_FAILED,
                                "Infrahub authentication failed",
                                context.operation,
                                safe_details={
                                    "host": endpoint_host,
                                    "status_code": resp.status_code,
                                    "auth_header_name": auth_header_name,
                                    "auth_attempt": auth_label,
                                    "auth_attempts": auth_attempts,
                                },
                            )
                        if resp.status_code == 429 or resp.status_code >= 500:
                            if attempt < retry_attempts - 1:
                                await asyncio.sleep(0.2 * (2**attempt))
                                should_retry = True
                                break
                            raise self._error(
                                ProviderErrorCode.UNAVAILABLE,
                                "Infrahub provider is unavailable",
                                context.operation,
                                retryable=True,
                            )
                        if resp.status_code == 404:
                            raise self._error(
                                ProviderErrorCode.NOT_FOUND,
                                "Infrahub resource not found",
                                context.operation,
                            )
                        if 400 <= resp.status_code < 500:
                            raise self._error(
                                ProviderErrorCode.MALFORMED_QUERY,
                                "Infrahub malformed query or bad request",
                                context.operation,
                            )
                        resp.raise_for_status()
                        data = resp.json()
                        if data.get("errors"):
                            raise self._error(
                                ProviderErrorCode.MALFORMED_QUERY,
                                "Infrahub GraphQL query failed",
                                context.operation,
                                safe_details=_safe_graphql_error_details(data.get("errors")),
                            )
                        if not isinstance(data.get("data"), dict):
                            raise self._error(
                                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                                "Infrahub response is malformed",
                                context.operation,
                            )
                        return data["data"]
                if should_retry:
                    continue
            except httpx.TimeoutException as exc:
                if attempt < retry_attempts - 1:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                raise InventoryProviderError.from_exception(
                    provider_id=self.id,
                    provider_type=self.provider_type,
                    operation=context.operation,
                    code=ProviderErrorCode.TIMEOUT,
                    message="Infrahub request timed out",
                    exc=exc,
                    retryable=True,
                ) from exc
            except httpx.TransportError as exc:
                if attempt < retry_attempts - 1:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                raise InventoryProviderError.from_exception(
                    provider_id=self.id,
                    provider_type=self.provider_type,
                    operation=context.operation,
                    code=ProviderErrorCode.UNAVAILABLE,
                    message="Infrahub transport failed",
                    exc=exc,
                    retryable=True,
                ) from exc
        raise self._error(
            ProviderErrorCode.UNAVAILABLE,
            "Infrahub provider request failed",
            context.operation,
            retryable=True,
        )

    def _extract_nodes(self, data: Mapping[str, Any]) -> builtins.list[Mapping[str, Any]]:
        root = data.get(self.config.device_kind) or next(
            (v for v in data.values() if isinstance(v, Mapping) and "edges" in v), None
        )
        edges = root.get("edges") if isinstance(root, Mapping) else []
        if not isinstance(edges, list):
            raise self._error(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "Infrahub pagination shape is missing edges",
                "query_devices",
            )
        return [
            edge.get("node")
            for edge in edges
            if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping)
        ]

    def _merged_access_config(self, node: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        defaults = (
            self.config.default_access_config
            if isinstance(self.config.default_access_config, Mapping)
            else {}
        )

        mapped_access_config: dict[str, Any] = {}
        for field_key in self._field_map:
            if field_key.startswith("access_config."):
                mapped_value = _lookup_path(node, self._field_map[field_key])
                if mapped_value is not None:
                    _assign_nested(
                        mapped_access_config,
                        field_key.removeprefix("access_config."),
                        mapped_value,
                    )

        # jump_host rides along with the credential scopes so a provider can
        # stamp a shared bastion hop on every device it yields
        # (design-dind-sandbox.md §11.2 in the platform repo).
        for scope in ("ssh", "enable", "jump_host"):
            scope_values: dict[str, Any] = {}

            default_scope = defaults.get(scope)
            if isinstance(default_scope, Mapping):
                scope_values.update(default_scope)

            mapped_scope = mapped_access_config.get(scope)
            if isinstance(mapped_scope, Mapping):
                scope_values.update(mapped_scope)

            if scope_values:
                merged[scope] = scope_values

        return merged

    def _map_device(self, node: Mapping[str, Any]) -> DeviceDescriptor:
        fm = self._field_map

        def _mapped_value(field_key: str, fallback: str | None = None) -> Any:
            path = fm.get(field_key) or fallback
            return _lookup_path(node, path) if path else None

        external_id = str(
            _mapped_value("external_id", DEFAULT_FIELD_MAP["external_id"]) or ""
        ).strip()
        name = str(
            _mapped_value("name", DEFAULT_FIELD_MAP["name"])
            or node.get("display_label")
            or external_id
        ).strip()
        mgmt_host = str(_mapped_value("mgmt_host", DEFAULT_FIELD_MAP["mgmt_host"]) or name).split(
            "/"
        )[0]
        # Map Infrahub's native management port; when absent, the core inventory
        # service applies the default during materialization.
        mgmt_port_value = _mapped_value("mgmt_port")
        try:
            mgmt_port = int(mgmt_port_value) if mgmt_port_value else None
        except (TypeError, ValueError):
            mgmt_port = None
        # Map Infrahub's native platform value; when absent, the core inventory
        # service applies the default during materialization.
        platform_value = _mapped_value("platform", DEFAULT_FIELD_MAP["platform"])
        platform = str(platform_value) if platform_value else None
        site_value = _mapped_value("site", DEFAULT_FIELD_MAP["site"])
        site = self._site_ref_from_value(site_value)
        role = _mapped_value("role", DEFAULT_FIELD_MAP["role"])
        raw_tags = _mapped_value("tags", DEFAULT_FIELD_MAP["tags"]) or []
        if not isinstance(raw_tags, list):
            raw_tags = [raw_tags]
        validated_access_config = self._services.validate_access_config(
            self._merged_access_config(node),
            operation="query_devices",
            allow_raw_literals=False,
        )
        return DeviceDescriptor(
            provider_id=self.id,
            external_id=external_id,
            name=name,
            display_name=name,
            hostname=name,
            mgmt_host=mgmt_host,
            mgmt_port=mgmt_port,
            platform=platform,
            site=site,
            role=str(role) if role else None,
            tags={"role": str(role)} if role else {},
            native_tags=tuple(str(t) for t in raw_tags if t),
            access_config=validated_access_config,
            descriptive={"branch": self.config.branch},
            source_version={"branch": self.config.branch},
        )

    def _site_ref_from_value(self, site_value: Any) -> SiteRef | None:
        if site_value is None:
            return None

        site_text = str(site_value).strip()
        if not site_text:
            return None

        return SiteRef(provider_id=self.id, external_id=site_text, name=site_text)

    def _matches_expr(self, descriptor: DeviceDescriptor, expr: str) -> bool:
        parsed = parse_qs(expr.lstrip("?"), keep_blank_values=False)
        ids = {
            value.strip()
            for raw in parsed.get("id", [])
            for value in raw.split(",")
            if value.strip()
        }
        if ids and descriptor.external_id not in ids:
            return False
        names = {
            value.strip()
            for raw in parsed.get("name", [])
            for value in raw.split(",")
            if value.strip()
        }
        return not (names and descriptor.name not in names)

    def _reject_duplicate_external_ids(
        self, descriptors: builtins.list[DeviceDescriptor], operation: str
    ) -> None:
        seen: set[str] = set()
        for descriptor in descriptors:
            if not descriptor.external_id or not descriptor.external_id.strip():
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "Infrahub device is missing a stable external id",
                    operation,
                )
            if descriptor.external_id in seen:
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "Infrahub response contains duplicate external ids",
                    operation,
                )
            seen.add(descriptor.external_id)

    def _error(
        self,
        code: ProviderErrorCode,
        message: str,
        operation: str,
        *,
        retryable: bool = False,
        safe_details: dict[str, Any] | None = None,
    ) -> InventoryProviderError:
        return InventoryProviderError(
            ProviderErrorEnvelope(
                provider_id=self.id,
                provider_type=self.provider_type,
                operation=operation,
                code=code,
                message=message,
                retryable=retryable,
                safe_details=safe_details or {},
            )
        )
