# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping
from dataclasses import replace
from time import monotonic
from typing import Any
from urllib.parse import ParseResult, quote, urlencode, urljoin, urlparse

import httpx

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    InventoryProvider,
    InventoryProviderError,
    MaintenanceState,
    ObjectDescriptor,
    PlatformServices,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderTestResult,
    ResourceRef,
    ResourceType,
    SiteRef,
    map_record,
    map_records,
)

from .config import DEFAULT_FIELD_MAP, NetBoxProviderConfig
from .object_types import ALL_OBJECT_TYPE_IDS, NetBoxObjectType, sources_for

KNOWN_AUTH_SCHEMES = frozenset({"bearer", "token", "basic"})


def _query_to_expr(query: Mapping[str, Any] | None) -> str:
    """Render a query mapping as a URL-encoded NetBox querystring.

    A sequence value repeats its key the way NetBox's filter DSL expects
    (``?tag=core&tag=wan``); ``None`` values are dropped rather than sent as the
    string "None".
    """
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            pairs.extend((str(key), str(item)) for item in value if item is not None)
        else:
            pairs.append((str(key), str(value)))
    return urlencode(pairs)


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


# NetBox keeps TWO independent nested hierarchies above a site -- Region
# (geography) and SiteGroup (function) -- and a site carries a nullable foreign
# key to each. A platform Site has a single ``parent_id``, so the two cannot both
# be projected onto it: combining them would mean inventing "Region x Group"
# nodes that exist in neither system. ``site_tree_source`` picks one; the other
# is surfaced as a site tag, so the choice never loses information.
#
# NetBox slugs are unique per model rather than globally -- a region and a site
# may both be "gondor" -- and the platform keys sites on (provider_id,
# external_id), so the synthesized nodes carry a prefix. It must be the same
# prefix everywhere: in the parent link, in the node's own id, and in _get_site.
#
# config value -> (site payload key, list endpoint, external-id prefix, node kind)
_SITE_TREE_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "region": ("region", "/api/dcim/regions/", "region:", "region"),
    "site_group": ("group", "/api/dcim/site-groups/", "site-group:", "site_group"),
}


class NetBoxInventoryProvider(InventoryProvider):
    provider_type = "netbox"
    capabilities = frozenset({Capability.READ, Capability.QUERY})
    # The class declares the whole catalogue, which is what the provider *type* can
    # serve and what the platform offers an operator to choose from. An instance
    # narrows it below to what its config actually reads.
    supported_resources = frozenset(
        {ResourceType.DEVICE.value, ResourceType.SITE.value, *ALL_OBJECT_TYPE_IDS}
    )
    config_schema = NetBoxProviderConfig.model_json_schema()

    def __init__(
        self, *, provider_id: str, config: dict[str, Any], services: PlatformServices
    ) -> None:
        self.id = provider_id
        self.config = NetBoxProviderConfig.model_validate(config or {})
        self._services = services
        # What THIS provider reads: the configured families, plus devices and sites,
        # which are read unconditionally. Shadowing the class attribute is what tells
        # the platform to skip the rest -- nothing is fetched for a family that is not
        # declared, and rows an earlier sync stored for one read stale rather than
        # staying active for ever.
        self._object_sources: dict[str, NetBoxObjectType] = sources_for(
            self.config.enabled_object_types
        )
        self.supported_resources = frozenset(
            {ResourceType.DEVICE.value, ResourceType.SITE.value, *self._object_sources}
        )

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
                for d in await self.query_devices(
                    _query_to_expr(query), limit=limit, context=context
                )
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
                for s in await self._sites_with_tree(
                    _query_to_expr(query), limit=limit, context=context
                )
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
            return await self._get_site(external_id, context)
        raise ValueError(f"Unsupported resource type: {resource_type}")

    async def _get_site(self, external_id: str, context: ProviderCallContext) -> SiteRef:
        """Read one site, resolving a non-numeric external id through a filter first.

        NetBox's detail route is keyed by the numeric primary key, but a site's
        external id follows ``field_map['site.external_id']`` — ``site.slug`` by
        default. Sending a slug to ``/api/dcim/sites/<slug>/`` is a 404, so
        anything other than the numeric ``id`` is looked up through the list
        endpoint and matched on the same key ``list_sites`` emitted.
        """
        identifier = str(external_id).strip()
        if not identifier:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                "NetBox site id is empty",
                context.operation,
            )
        # A hierarchy node is resolved from its own endpoint. Every prefix is
        # tried, not only the configured one: a region row materialized under an
        # earlier `site_tree_source` still has to open rather than 404 after the
        # setting changes.
        node = await self._get_tree_node(identifier, context)
        if node is not None:
            return node
        id_path = self._site_id_path()
        if id_path == "id":
            payload = await self._request_json(
                f"/api/dcim/sites/{quote(identifier, safe='')}/", context
            )
            return self._map_site(payload)
        # A dotted path has no NetBox filter of the same name, so only the leaf
        # is usable as one; `slug` and `name` — the realistic keys — are leaves.
        field = id_path.rpartition(".")[2]
        matches = await self._fetch_sites(
            _query_to_expr({field: identifier}), limit=2, context=context
        )
        for site in matches:
            if site.external_id == identifier:
                return site
        raise self._error(
            ProviderErrorCode.NOT_FOUND,
            f"No NetBox site with {field}={identifier!r}",
            context.operation,
        )

    async def _get_tree_node(self, identifier: str, context: ProviderCallContext) -> SiteRef | None:
        """Resolve a synthesized hierarchy id, or None when it is not one."""
        for _payload_key, path, prefix, kind in _SITE_TREE_SOURCES.values():
            if not identifier.startswith(prefix):
                continue
            slug = identifier[len(prefix) :]
            items = await self._paginated(
                f"{path}?slug={quote(slug, safe='')}",
                context,
                page_limit=1,
                result_limit=2,
            )
            for item in items:
                if _optional_str(item.get("slug")) == slug:
                    return self._map_tree_node(item, prefix=prefix, kind=kind)
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                f"No NetBox {kind} with slug={slug!r}",
                context.operation,
            )
        return None

    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        page_limit = self._services.limits.max_provider_pages
        result_limit = limit or self._services.limits.max_run_targets
        path = "/api/dcim/devices/"
        # The configured baseline narrows every device read (auto-sync calls this
        # with an empty expression, so without it a sync pulls the whole table);
        # a caller expression is appended, and NetBox ANDs the two.
        parts = [part for part in (self.config.device_query, expr.lstrip("?")) if part]
        if parts:
            path = f"{path}?{'&'.join(parts)}"
        items = await self._paginated(
            path, context, page_limit=page_limit, result_limit=result_limit
        )
        descriptors = [self._map_device(item) for item in items]
        self._reject_duplicate_external_ids(descriptors, context.operation)
        return descriptors

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        return await self._sites_with_tree("", limit=limit, context=context)

    async def _sites_with_tree(
        self, expr: str, *, limit: int | None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        """The sites, preceded by the hierarchy nodes they hang from.

        Both halves go out together on every call, and that is not incidental:
        the platform marks any provider site a sync stops reporting as stale, so
        a node emitted once and then omitted would flap. It is also why the nodes
        are recomputed from the sites each time rather than cached.

        The nodes are extra, on top of ``limit``. The limit is a bound on how many
        *sites* a caller asked for, and returning a site whose parent was trimmed
        away would flatten it for no reason the caller could see.
        """
        payloads = await self._fetch_site_payloads(expr, limit=limit, context=context)
        nodes = await self._site_tree_nodes(payloads, context)
        return nodes + [self._map_site(item) for item in payloads]

    async def _fetch_sites(
        self, expr: str, *, limit: int | None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        """The sites alone, with no hierarchy nodes.

        Used where a caller is resolving one known site rather than browsing, so
        the extra request for the hierarchy would buy nothing.
        """
        payloads = await self._fetch_site_payloads(expr, limit=limit, context=context)
        return [self._map_site(item) for item in payloads]

    async def _fetch_site_payloads(
        self, expr: str, *, limit: int | None, context: ProviderCallContext
    ) -> builtins.list[dict[str, Any]]:
        """Raw site payloads, optionally narrowed by a NetBox filter expression."""
        path = "/api/dcim/sites/"
        query = expr.lstrip("?")
        if query:
            path = f"{path}?{query}"
        return await self._paginated(
            path,
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=limit or self._services.limits.max_preview_devices,
        )

    async def _site_tree_nodes(
        self, payloads: builtins.list[dict[str, Any]], context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        """The ancestor closure of the hierarchy nodes these sites belong to.

        Only ancestors of the sites actually synced are emitted. NetBox's region
        tree can describe the whole world, and a provider scoped to six sites has
        no business dragging all of it into the platform's inventory.
        """
        source = _SITE_TREE_SOURCES.get(self.config.site_tree_source)
        if source is None:
            return []
        payload_key, path, prefix, kind = source

        referenced = {
            slug
            for item in payloads
            if isinstance(item.get(payload_key), Mapping)
            and (slug := _optional_str(item[payload_key].get("slug")))
        }
        if not referenced:
            return []

        items = await self._paginated(
            path,
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=self._services.limits.max_preview_devices,
        )
        by_slug = {
            slug: item for item in items if (slug := _optional_str(item.get("slug"))) is not None
        }

        wanted: set[str] = set()
        for slug in referenced:
            cursor: str | None = slug
            # NetBox's own tree cannot contain a cycle, but this walks data from
            # a remote system and a cycle here would hang the sync rather than
            # fail it, so the walk stops at the first repeat.
            while cursor is not None and cursor in by_slug and cursor not in wanted:
                wanted.add(cursor)
                parent = by_slug[cursor].get("parent")
                cursor = _optional_str(parent.get("slug")) if isinstance(parent, Mapping) else None

        return [
            self._map_tree_node(by_slug[slug], prefix=prefix, kind=kind) for slug in sorted(wanted)
        ]

    def _map_tree_node(self, item: Mapping[str, Any], *, prefix: str, kind: str) -> SiteRef:
        """One region or site group, as a site that other sites hang from.

        ``node_type`` is carried in both tags and descriptive so that a selector
        or a UI can tell an aggregation node from a place with equipment in it.
        """
        slug = _optional_str(item.get("slug")) or ""
        parent = item.get("parent")
        parent_slug = _optional_str(parent.get("slug")) if isinstance(parent, Mapping) else None
        descriptive: dict[str, Any] = {"slug": slug, "node_type": kind}
        provider_url = self._provider_url(item)
        if provider_url:
            descriptive["provider_url"] = provider_url
        return SiteRef(
            provider_id=self.id,
            external_id=f"{prefix}{slug}",
            name=str(item.get("name") or item.get("display") or slug),
            parent_external_id=f"{prefix}{parent_slug}" if parent_slug else None,
            location=_optional_str(item.get("description")),
            tags={"node_type": kind},
            descriptive=descriptive,
        )

    async def list_objects(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ObjectDescriptor]:
        source = self._object_sources.get(object_type)
        if source is None:
            # device/site (and a family this provider is not configured to read) fall
            # back to the SDK default, which delegates to query_devices/list_sites or
            # raises "not supported".
            return await super().list_objects(
                object_type, query, limit=limit, cursor=cursor, context=context
            )
        items = await self._paginated(
            self._object_path(source, _query_to_expr(query)),
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=limit or self._services.limits.max_run_targets,
        )
        return [
            self._with_provider_url(descriptor, item)
            for descriptor, item in zip(
                map_records(items, source.mapping, provider_id=self.id), items, strict=True
            )
        ]

    def _object_scope(self, source: NetBoxObjectType) -> str:
        """The filter this provider always applies to a family, built in and configured.

        This is the whole of what a provider is allowed to see of a family, so every read
        of it -- a list or a single object -- has to go through this.
        """
        configured = self.config.object_queries.get(source.id, "")
        return "&".join(part for part in (source.base_query, configured) if part)

    def _object_path(self, source: NetBoxObjectType, expr: str) -> str:
        """The endpoint for one family, scoped by its configured and caller filters.

        Same shape as ``device_query``: the configured baseline narrows every read of
        the family, a caller expression is appended, and NetBox ANDs the two.
        """
        parts = [part for part in (self._object_scope(source), expr.lstrip("?")) if part]
        return f"{source.endpoint}?{'&'.join(parts)}" if parts else source.endpoint

    async def get_object(
        self,
        object_type: str,
        external_id: str,
        *,
        context: ProviderCallContext,
    ) -> ObjectDescriptor:
        """Fetch one object of ``object_type``.

        Served from the same catalogue as ``list_objects``; without this override the
        generic families fell through to the SDK default, which only knows device and
        site, so every object detail page raised "not supported".
        """
        source = self._object_sources.get(object_type)
        if source is None:
            return await super().get_object(object_type, external_id, context=context)
        identifier = str(external_id).strip()
        if not identifier:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                "NetBox object id is empty",
                context.operation,
            )
        payload = await self._object_payload(source, identifier, context)
        return self._with_provider_url(
            map_record(payload, source.mapping, provider_id=self.id), payload
        )

    async def _object_payload(
        self, source: NetBoxObjectType, identifier: str, context: ProviderCallContext
    ) -> dict[str, Any]:
        """One object's raw payload, honouring the filter that scopes its family.

        NetBox's detail endpoint takes no filters, so reading it directly would hand back
        an object from outside the configured ``object_queries`` scope to any caller that
        knows its id -- a provider narrowed to one site answering for another. Where a
        scope is configured the id is resolved through the filtered list endpoint instead
        (``id`` is a filter every NetBox family supports), which leaves an object outside
        the scope indistinguishable from one that does not exist. Unscoped families keep
        the cheaper direct read.
        """
        if not self._object_scope(source):
            return await self._request_json(
                f"{source.endpoint}{quote(identifier, safe='')}/", context
            )
        matches = await self._paginated(
            self._object_path(source, _query_to_expr({"id": identifier})),
            context,
            page_limit=self._services.limits.max_provider_pages,
            # `id` is NetBox's primary key, so at most one row can match; asking for two
            # would make the pager chase a second page that cannot hold a match.
            result_limit=1,
        )
        if not matches:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                f"NetBox {source.display_name} {identifier} was not found within this "
                "provider's configured object query",
                context.operation,
            )
        return matches[0]

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
        descriptive: dict[str, Any] = {
            "netbox_status": item.get("status"),
            "native_tags": list(native_tags),
        }
        # NetBox facts the platform's device row has no column for. They stay
        # searchable (descriptive is GIN-indexed) and visible over the API.
        for field_key in ("serial", "asset_tag", "rack", "position", "tenant", "location"):
            value = self._mapped_value(item, field_key, DEFAULT_FIELD_MAP.get(field_key))
            if value is not None:
                descriptive[field_key] = value
        mgmt_host_v6 = _optional_str(
            self._mapped_value(item, "mgmt_host_v6", DEFAULT_FIELD_MAP["mgmt_host_v6"])
        )
        if mgmt_host_v6:
            descriptive["mgmt_host_v6"] = mgmt_host_v6.split("/")[0]
        provider_url = self._provider_url(item)
        if provider_url:
            descriptive["provider_url"] = provider_url
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
            vendor=_optional_str(self._mapped_value(item, "vendor", DEFAULT_FIELD_MAP["vendor"])),
            model=model,
            site=site,
            role=str(role) if role else None,
            tags=self._device_tags(role, native_tags),
            native_tags=native_tags,
            access_config=validated_access_config,
            descriptive=descriptive,
            maintenance_state=self._maintenance_state(item),
        )

    def _with_provider_url(
        self, descriptor: ObjectDescriptor, item: Mapping[str, Any]
    ) -> ObjectDescriptor:
        """Attach the object's NetBox UI URL, so generic pages can link back."""
        provider_url = self._provider_url(item)
        if not provider_url:
            return descriptor
        return replace(
            descriptor, descriptive={**descriptor.descriptive, "provider_url": provider_url}
        )

    def _provider_url(self, item: Mapping[str, Any]) -> str | None:
        """The NetBox UI URL for one object, for an "Open in NetBox" link.

        Preferred source is NetBox's own ``display_url``; older versions only
        carry the API ``url``, whose ``/api`` prefix is dropped to reach the UI
        page. Both are pinned to the configured origin, so a NetBox that echoes
        someone else's host cannot turn the link into an off-site redirect.
        """
        for key in ("display_url", "url"):
            candidate = _optional_str(item.get(key))
            if not candidate:
                continue
            parsed = urlparse(candidate)
            if self._origin(parsed) != self._origin(urlparse(str(self.config.url))):
                continue
            path = parsed.path.replace("/api/", "/", 1) if key == "url" else parsed.path
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        return None

    def _site_id_path(self) -> str:
        """The site-payload path holding a site's external id.

        Derived from the same ``field_map['site.external_id']`` entry ``_map_device``
        reads, minus its leading ``site.`` segment, so a site fetched from
        ``/api/dcim/sites/`` and the same site nested inside a device key on the
        identical value. When the two disagreed (numeric ``id`` here, ``slug``
        there) the platform materialized every NetBox site twice and attached
        devices to the copy that carried no parent link or location.
        """
        path = (
            self.config.field_map.get("site.external_id") or DEFAULT_FIELD_MAP["site.external_id"]
        )
        prefix, separator, remainder = path.partition(".")
        if separator and prefix == "site" and remainder:
            return remainder
        return path

    def _site_external_id(self, item: Mapping[str, Any]) -> str | None:
        """Resolve one site payload's external id through :meth:`_site_id_path`."""
        return (
            _optional_str(_lookup_path(item, self._site_id_path()))
            or _optional_str(item.get("id"))
            or _optional_str(item.get("slug"))
            or _optional_str(item.get("name"))
        )

    def _maintenance_state(self, item: Mapping[str, Any]) -> MaintenanceState | None:
        """Map the NetBox device status onto the platform's maintenance state.

        NetBox statuses arrive as ``{"value": "offline", "label": "Offline"}``, or
        as a bare slug on older payloads. An unmapped status (``active``, or one a
        deployment has not configured) yields ``None``, which leaves the device's
        existing state untouched rather than forcing it back to active.
        """
        raw = item.get("status")
        slug = raw.get("value") if isinstance(raw, Mapping) else raw
        status = _optional_str(slug)
        if status is None:
            return None
        mapped = self.config.status_map.get(status.lower())
        return MaintenanceState(mapped) if mapped else None

    def _device_tags(self, role: Any, native_tags: tuple[str, ...]) -> dict[str, str]:
        """Build the filterable ``key=value`` tag map for one device.

        NetBox tags are flat slugs, but the platform filters devices on a
        ``key=value`` map (``tags[role]=core`` in device search, ``tag.*`` in run
        forms). Each NetBox tag is therefore surfaced as ``<prefix><slug>=true``.
        The prefix keeps a tag named ``role`` from colliding with the role entry
        and is configurable via ``tag_key_prefix``; set it to an empty string to
        map NetBox tags onto bare keys.

        An already-present key always wins, which is what keeps the mapped role
        authoritative when the prefix is empty and NetBox happens to carry a tag
        named ``role``. Without that, ``tags[role]`` would read ``true`` instead
        of the device's role and every role filter would miss the device.
        """
        tags: dict[str, str] = {}
        if role:
            tags["role"] = str(role)
        prefix = self.config.tag_key_prefix
        for slug in native_tags:
            key = f"{prefix}{slug}".strip()
            if slug and key and key not in tags:
                tags[key] = "true"
        return tags

    def _map_site(self, item: Mapping[str, Any]) -> SiteRef:
        external_id = self._site_external_id(item) or ""
        return SiteRef(
            provider_id=self.id,
            external_id=external_id,
            name=str(item.get("name") or item.get("display") or external_id),
            parent_external_id=self._site_parent_external_id(item),
            location=str(item.get("physical_address") or item.get("description") or "") or None,
            tags=self._site_tags(item),
            descriptive=self._site_descriptive(item),
        )

    def _site_parent_external_id(self, item: Mapping[str, Any]) -> str | None:
        """Where this site hangs in the tree, per ``site_tree_source``.

        A NetBox site has no ``parent`` of its own -- the hierarchy is the region
        or the site group it points at -- so the id returned here is the
        synthesized one ``_map_tree_node`` gives that node, prefix included.
        """
        source = _SITE_TREE_SOURCES.get(self.config.site_tree_source)
        if source is not None:
            payload_key, _, prefix, _kind = source
            node = item.get(payload_key)
            if isinstance(node, Mapping) and (slug := _optional_str(node.get("slug"))):
                return f"{prefix}{slug}"
        # A NetBox site carries no `parent` today. Honour one if a future version
        # or a compatible API grows it, rather than silently dropping it.
        parent = item.get("parent")
        return self._site_external_id(parent) if isinstance(parent, Mapping) else None

    def _site_tags(self, item: Mapping[str, Any]) -> dict[str, str]:
        """Both hierarchy axes, as filterable tags.

        Emitted whichever axis was chosen for the tree, which is the point: the
        one that did not become the tree is still the way an operator selects
        "every branch site" or "everything in Rohan". Bare keys rather than
        ``tag_key_prefix`` ones, because these are structural NetBox fields
        rather than NetBox tags -- the same reason a device's ``role`` is bare.
        """
        tags: dict[str, str] = {}
        for key in ("region", "group"):
            node = item.get(key)
            if isinstance(node, Mapping) and (slug := _optional_str(node.get("slug"))):
                tags[key] = slug
        return tags

    def _site_descriptive(self, item: Mapping[str, Any]) -> dict[str, Any]:
        descriptive: dict[str, Any] = {"slug": item.get("slug")}
        provider_url = self._provider_url(item)
        if provider_url:
            descriptive["provider_url"] = provider_url
        return descriptive

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
