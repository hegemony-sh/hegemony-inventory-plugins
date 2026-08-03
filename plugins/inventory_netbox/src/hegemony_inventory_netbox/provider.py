# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping
from time import monotonic
from typing import Any
from urllib.parse import ParseResult, urljoin, urlparse

import httpx

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    InventoryProvider,
    InventoryProviderError,
    ObjectDescriptor,
    ObjectFieldMapping,
    PlatformServices,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderTestResult,
    ResourceRef,
    ResourceType,
    SiteRef,
    map_records,
)

from .config import DEFAULT_FIELD_MAP, NetBoxProviderConfig

KNOWN_AUTH_SCHEMES = frozenset({"bearer", "token", "basic"})


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


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


# IPAM/VLAN object types served beyond device/site. Each entry pairs a NetBox API
# endpoint with a declarative record -> ObjectDescriptor mapping (see ``map_records``);
# adding a new type is one more entry here plus a matching ObjectTypeSpec in __init__.
_IPAM_OBJECT_SOURCES: dict[str, tuple[str, ObjectFieldMapping]] = {
    ResourceType.IP_PREFIX.value: (
        "/api/ipam/prefixes/",
        ObjectFieldMapping(
            object_type=ResourceType.IP_PREFIX.value,
            identity="id",
            name_field="prefix",
            field_map={
                "prefix": "prefix",
                "status": "status.value",
                "vlan": "vlan.vid",
                "role": "role.slug",
                "site": "site.slug",
                "tenant": "tenant.slug",
                "is_pool": "is_pool",
                "description": "description",
            },
        ),
    ),
    ResourceType.IP_ADDRESS.value: (
        "/api/ipam/ip-addresses/",
        ObjectFieldMapping(
            object_type=ResourceType.IP_ADDRESS.value,
            identity="id",
            name_field="address",
            field_map={
                "address": "address",
                "status": "status.value",
                "role": "role.value",
                "dns_name": "dns_name",
                "vrf": "vrf.name",
                "tenant": "tenant.slug",
                "description": "description",
            },
        ),
    ),
    ResourceType.VLAN.value: (
        "/api/ipam/vlans/",
        ObjectFieldMapping(
            object_type=ResourceType.VLAN.value,
            identity="id",
            name_field="name",
            field_map={
                "vid": "vid",
                "name": "name",
                "status": "status.value",
                "site": "site.slug",
                "group": "group.name",
                "role": "role.slug",
                "tenant": "tenant.slug",
                "description": "description",
            },
        ),
    ),
}


class NetBoxInventoryProvider(InventoryProvider):
    provider_type = "netbox"
    capabilities = frozenset({Capability.READ, Capability.QUERY})
    supported_resources = frozenset(
        {
            ResourceType.DEVICE,
            ResourceType.SITE,
            ResourceType.IP_PREFIX,
            ResourceType.IP_ADDRESS,
            ResourceType.VLAN,
        }
    )
    config_schema = NetBoxProviderConfig.model_json_schema()

    def __init__(
        self, *, provider_id: str, config: dict[str, Any], services: PlatformServices
    ) -> None:
        self.id = provider_id
        self.config = NetBoxProviderConfig.model_validate(config or {})
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
        self,
        resource_type: ResourceType,
        external_id: str,
        *,
        context: ProviderCallContext,
    ) -> DeviceDescriptor | SiteRef | ResourceRef:
        if resource_type == ResourceType.DEVICE:
            payload = await self._request_json(f"/api/dcim/devices/{external_id}/", context)
            return self._map_device(payload)
        if resource_type == ResourceType.SITE:
            payload = await self._request_json(f"/api/dcim/sites/{external_id}/", context)
            return self._map_site(payload)
        raise ValueError(f"Unsupported resource type: {resource_type}")

    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        page_limit = self._services.limits.max_provider_pages
        result_limit = limit or self._services.limits.max_run_targets
        path = "/api/dcim/devices/"
        query = expr.lstrip("?")
        if query:
            path = f"{path}?{query}"
        items = await self._paginated(
            path, context, page_limit=page_limit, result_limit=result_limit
        )
        descriptors = [self._map_device(item) for item in items]
        self._reject_duplicate_external_ids(descriptors, context.operation)
        return descriptors

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        items = await self._paginated(
            "/api/dcim/sites/",
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=limit or self._services.limits.max_preview_devices,
        )
        return [self._map_site(item) for item in items]

    async def list_objects(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ObjectDescriptor]:
        source = _IPAM_OBJECT_SOURCES.get(object_type)
        if source is None:
            # device/site (and anything else) fall back to the SDK default, which
            # delegates to query_devices/list_sites or raises "not supported".
            return await super().list_objects(
                object_type, query, limit=limit, cursor=cursor, context=context
            )
        endpoint, mapping = source
        items = await self._paginated(
            endpoint,
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=limit or self._services.limits.max_run_targets,
        )
        return map_records(items, mapping, provider_id=self.id)

    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        start = monotonic()
        try:
            await self._services.validate_url(str(self.config.url), operation="test_connection")
            await self._request_json("/api/dcim/devices/?limit=1", context)
            ok = True
            error = None
            message = "NetBox connection succeeded"
        except InventoryProviderError as exc:
            ok = False
            error = exc.envelope.to_dict()
            message = exc.envelope.message
        return ProviderTestResult(
            ok=ok,
            provider_id=self.id,
            provider_type=self.provider_type,
            latency_ms=int((monotonic() - start) * 1000),
            message=message,
            error=error,
        )

    async def _request_json(self, path_or_url: str, context: ProviderCallContext) -> dict[str, Any]:
        await self._services.validate_url(str(self.config.url), operation=context.operation)
        url = self._request_url(path_or_url, context.operation)
        token = await self._services.resolve_secret_ref(
            self.config.token_ref, operation=context.operation
        )
        authorization_values = self._authorization_header_values(token)
        retry_attempts = 4
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_tls,
            follow_redirects=False,
        ) as client:
            for attempt in range(retry_attempts):
                for auth_index, authorization in enumerate(authorization_values):
                    headers = {"Authorization": authorization} if authorization else {}
                    try:
                        resp = await client.get(url, headers=headers)
                        if (
                            resp.status_code in {401, 403}
                            and auth_index < len(authorization_values) - 1
                        ):
                            continue
                        if resp.status_code in {401, 403}:
                            raise self._error(
                                ProviderErrorCode.AUTH_FAILED,
                                "NetBox authentication failed",
                                context.operation,
                            )
                        if resp.status_code == 404:
                            raise self._error(
                                ProviderErrorCode.NOT_FOUND,
                                "NetBox resource not found",
                                context.operation,
                            )
                        if resp.status_code == 429 or resp.status_code >= 500:
                            if attempt < retry_attempts - 1:
                                await asyncio.sleep(0.2 * (2**attempt))
                                break
                            raise self._error(
                                ProviderErrorCode.UNAVAILABLE,
                                "NetBox provider is unavailable",
                                context.operation,
                                retryable=True,
                            )
                        if 400 <= resp.status_code < 500:
                            raise self._client_error(resp, context.operation)
                        try:
                            data = resp.json()
                        except ValueError as exc:
                            raise InventoryProviderError.from_exception(
                                provider_id=self.id,
                                provider_type=self.provider_type,
                                operation=context.operation,
                                code=ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                                message="NetBox response was not valid JSON",
                                exc=exc,
                            ) from exc
                        if not isinstance(data, dict):
                            raise self._error(
                                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                                "NetBox response was not a JSON object",
                                context.operation,
                            )
                        return data
                    except httpx.TimeoutException as exc:
                        if attempt < retry_attempts - 1:
                            await asyncio.sleep(0.2 * (2**attempt))
                            break
                        raise InventoryProviderError.from_exception(
                            provider_id=self.id,
                            provider_type=self.provider_type,
                            operation=context.operation,
                            code=ProviderErrorCode.TIMEOUT,
                            message="NetBox request timed out",
                            exc=exc,
                            retryable=True,
                        ) from exc
                    except httpx.TransportError as exc:
                        if attempt < retry_attempts - 1:
                            await asyncio.sleep(0.2 * (2**attempt))
                            break
                        raise InventoryProviderError.from_exception(
                            provider_id=self.id,
                            provider_type=self.provider_type,
                            operation=context.operation,
                            code=ProviderErrorCode.UNAVAILABLE,
                            message="NetBox transport failed",
                            exc=exc,
                            retryable=True,
                        ) from exc
        raise self._error(
            ProviderErrorCode.UNAVAILABLE,
            "NetBox provider request failed",
            context.operation,
            retryable=True,
        )

    def _request_url(self, path_or_url: str, operation: str) -> str:
        base_url = str(self.config.url)
        parsed = urlparse(path_or_url)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "NetBox pagination URL is malformed",
                    operation,
                )
            if self._origin(parsed) != self._origin(urlparse(base_url)):
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "NetBox pagination URL must stay on the configured provider origin",
                    operation,
                    safe_details={"host": parsed.hostname},
                )
            return path_or_url
        return urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))

    def _origin(self, parsed_url: ParseResult) -> tuple[str, str, int | None]:
        port = parsed_url.port
        if port is None and parsed_url.scheme == "https":
            port = 443
        elif port is None and parsed_url.scheme == "http":
            port = 80
        return (parsed_url.scheme, parsed_url.hostname or "", port)

    def _authorization_header_values(self, token: str | None) -> builtins.list[str | None]:
        if token is None:
            return [None]
        stripped = token.strip()
        if not stripped:
            return [None]
        prefix, _, remainder = stripped.partition(" ")
        if remainder and prefix.lower() in KNOWN_AUTH_SCHEMES:
            return [stripped]
        schemes = [self.config.auth_scheme, *self.config.auth_fallback_schemes]
        seen: set[str] = set()
        headers: builtins.list[str | None] = []
        for scheme in schemes:
            normalized = scheme.strip()
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            headers.append(f"{normalized} {stripped}")
        if headers:
            return headers
        return [stripped]

    def _client_error(self, response: httpx.Response, operation: str) -> InventoryProviderError:
        status_code = response.status_code
        safe_details = {"status_code": status_code}
        if status_code in {401, 403}:
            return self._error(
                ProviderErrorCode.AUTH_FAILED,
                "NetBox authentication failed",
                operation,
                safe_details=safe_details,
            )
        if status_code == 404:
            return self._error(
                ProviderErrorCode.NOT_FOUND,
                "NetBox resource not found",
                operation,
                safe_details=safe_details,
            )
        if status_code in {400, 422}:
            return self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "NetBox rejected the query or request",
                operation,
                safe_details=safe_details,
            )
        return self._error(
            ProviderErrorCode.UNAVAILABLE,
            f"NetBox provider returned HTTP {status_code}",
            operation,
            safe_details=safe_details,
        )

    async def _paginated(
        self, path: str, context: ProviderCallContext, *, page_limit: int, result_limit: int
    ) -> builtins.list[dict[str, Any]]:
        items: builtins.list[dict[str, Any]] = []
        next_url: str | None = path
        pages = 0
        while next_url:
            pages += 1
            if pages > page_limit:
                raise self._error(
                    ProviderErrorCode.PAGINATION_FAILED,
                    "NetBox pagination limit exceeded",
                    context.operation,
                )
            page = await self._request_json(next_url, context)
            raw_results = page.get("results", page if isinstance(page, list) else [])
            if not isinstance(raw_results, list):
                raise self._error(
                    ProviderErrorCode.PAGINATION_FAILED,
                    "NetBox pagination response is malformed",
                    context.operation,
                )
            items.extend([item for item in raw_results if isinstance(item, dict)])
            if len(items) >= result_limit:
                return items[:result_limit]
            next_raw = page.get("next") if isinstance(page, dict) else None
            next_url = next_raw if isinstance(next_raw, str) and next_raw else None
        return items

    def _mapped_value(
        self,
        item: Mapping[str, Any],
        field_key: str,
        fallback: str | None = None,
    ) -> Any:
        path = self.config.field_map.get(field_key) or fallback
        return _lookup_path(item, path) if path else None

    def _merged_access_config(self, item: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        defaults = (
            self.config.default_access_config
            if isinstance(self.config.default_access_config, Mapping)
            else {}
        )

        mapped_access_config: dict[str, Any] = {}
        for field_key, default_path in DEFAULT_FIELD_MAP.items():
            if field_key.startswith("access_config."):
                mapped_value = self._mapped_value(item, field_key, default_path)
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

    def _map_device(self, item: Mapping[str, Any]) -> DeviceDescriptor:
        external_id = str(
            self._mapped_value(item, "external_id", DEFAULT_FIELD_MAP["external_id"])
            or item.get("name")
            or ""
        ).strip()
        name = str(
            self._mapped_value(item, "name", DEFAULT_FIELD_MAP["name"])
            or self._mapped_value(item, "display_name", DEFAULT_FIELD_MAP["display_name"])
            or external_id
        ).strip()
        mgmt_host = str(
            self._mapped_value(item, "mgmt_host", DEFAULT_FIELD_MAP["mgmt_host"])
            or _lookup_path(item, "primary_ip.address")
            or item.get("mgmt_host")
            or self._mapped_value(item, "hostname", DEFAULT_FIELD_MAP["hostname"])
            or name
        ).split("/")[0]
        if not mgmt_host:
            mgmt_host = name
        platform = _optional_str(
            self._mapped_value(item, "platform", DEFAULT_FIELD_MAP["platform"])
        )
        model = _optional_str(self._mapped_value(item, "model", DEFAULT_FIELD_MAP["model"]))
        site_external_id = _optional_str(
            self._mapped_value(item, "site.external_id", DEFAULT_FIELD_MAP["site.external_id"])
        )
        site_name = (
            _optional_str(self._mapped_value(item, "site.name", DEFAULT_FIELD_MAP["site.name"]))
            or site_external_id
        )
        site = (
            SiteRef(
                provider_id=self.id,
                external_id=site_external_id,
                name=site_name or site_external_id,
            )
            if site_external_id
            else self._map_site(item["site"])
            if isinstance(item.get("site"), Mapping)
            else None
        )
        role = _optional_str(
            self._mapped_value(item, "role", DEFAULT_FIELD_MAP["role"])
            or _lookup_path(item, "device_role.slug")
        )
        validated_access_config = self._services.validate_access_config(
            self._merged_access_config(item),
            operation="query_devices",
            allow_raw_literals=False,
        )
        raw_tags = self._mapped_value(item, "tags", DEFAULT_FIELD_MAP["tags"]) or item.get("tags")
        tags: builtins.list[Any] = raw_tags if isinstance(raw_tags, list) else []
        native_tags = tuple(
            str(tag.get("slug") or tag.get("name") or tag) if isinstance(tag, Mapping) else str(tag)
            for tag in tags
        )
        descriptive = {"netbox_status": item.get("status"), "native_tags": list(native_tags)}
        return DeviceDescriptor(
            provider_id=self.id,
            external_id=external_id,
            name=name,
            display_name=str(
                self._mapped_value(item, "display_name", DEFAULT_FIELD_MAP["display_name"]) or name
            ),
            hostname=_optional_str(
                self._mapped_value(item, "hostname", DEFAULT_FIELD_MAP["hostname"])
            )
            or name,
            mgmt_host=mgmt_host,
            # NetBox has no device-level management port; omit it and let the core
            # inventory service apply the default during materialization.
            mgmt_port=None,
            # Map NetBox's native platform slug; when absent, the core inventory
            # service applies the default during materialization.
            platform=platform,
            vendor=None,
            model=model,
            site=site,
            role=str(role) if role else None,
            tags={"role": str(role)} if role else {},
            native_tags=native_tags,
            access_config=validated_access_config,
            descriptive=descriptive,
        )

    def _map_site(self, item: Mapping[str, Any]) -> SiteRef:
        external_id = str(item.get("id") or item.get("slug") or item.get("name") or "")
        parent = item.get("parent") if isinstance(item.get("parent"), Mapping) else None
        return SiteRef(
            provider_id=self.id,
            external_id=external_id,
            name=str(item.get("name") or item.get("display") or external_id),
            parent_external_id=str(parent.get("id") or parent.get("slug")) if parent else None,
            location=str(item.get("physical_address") or item.get("description") or "") or None,
            descriptive={"slug": item.get("slug")},
        )

    def _reject_duplicate_external_ids(
        self, descriptors: builtins.list[DeviceDescriptor], operation: str
    ) -> None:
        seen: set[str] = set()
        for descriptor in descriptors:
            if not descriptor.external_id or not descriptor.external_id.strip():
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "NetBox device is missing a stable external id",
                    operation,
                )
            if descriptor.external_id in seen:
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "NetBox response contains duplicate external ids",
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
