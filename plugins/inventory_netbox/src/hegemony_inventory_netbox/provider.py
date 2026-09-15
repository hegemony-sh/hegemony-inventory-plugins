# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any
from urllib.parse import ParseResult, quote, urlencode, urljoin, urlparse

import httpx

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    DiscoveryResult,
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
    map_record,
    map_records,
)

from .bindings import bindings_for, unsupported_field_map_keys
from .config import DEFAULT_FIELD_MAP, NetBoxProviderConfig
from .derive import (
    DerivedCatalogue,
    DiscoveryReport,
    SourceStatus,
    compare_to_catalogue,
    derive_catalogue,
    read_status,
    registered_catalogue,
)
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


def _with_discovered_paths(
    shipped: ObjectFieldMapping, discovered: ObjectFieldMapping | None
) -> ObjectFieldMapping:
    """``shipped``, plus the field paths only this instance knows about.

    A custom field is defined on one NetBox and not the next, and a release adds
    properties to families the recorded schema already had. Both show up in what an
    instance says it serves, and the platform puts them in the manifest -- so a read that
    ignored them would offer a browse column that is empty for every row.

    Only the field map is combined. Everything else on the mapping is a contract the
    plugin owns: ``identity`` is the key rows are already stored under, and the endpoint
    and base query are how this provider reaches the family. A discovery that disagreed
    about those would not be adding to what the plugin knows, it would be replacing it.
    """
    if discovered is None or not discovered.field_map:
        return shipped
    field_map = {**discovered.field_map, **shipped.field_map}
    if field_map == dict(shipped.field_map):
        return shipped
    return replace(shipped, field_map=field_map)


@dataclass(frozen=True, slots=True)
class _FetchPlan:
    """Everything needed to read one NetBox family: where it lives and how to map it.

    A catalogue family fills this from the catalogue. A family the instance *discovered*
    fills it from the mapping the platform hands back, which is the only place that
    knowledge survives -- the provider is built fresh per operation and the catalogue
    never knew the family in the first place.
    """

    object_type: str
    endpoint: str
    mapping: ObjectFieldMapping
    base_query: str = ""


class NetBoxInventoryProvider(InventoryProvider):
    provider_type = "netbox"
    capabilities = frozenset({Capability.READ, Capability.QUERY, Capability.DISCOVER})
    # Every family in the catalogue, plus devices and sites. This is the whole space
    # the provider can read, and it is deliberately not narrowed per instance: which
    # of them a given provider actually syncs is the platform's own object-type
    # selection, and a second narrowing here is what once made a ticked type sync
    # nothing at all.
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
        self._object_sources: dict[str, NetBoxObjectType] = sources_for(ALL_OBJECT_TYPE_IDS)

    async def list(
        self,
        resource_type: ResourceType,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ResourceRef]:
        """Devices and sites are object families here; everything else is unsupported."""
        if resource_type in (ResourceType.DEVICE, ResourceType.SITE):
            raise self._read_them_as_objects(context.operation)
        return []

    async def get(
        self,
        resource_type: ResourceType,
        external_id: str,
        *,
        context: ProviderCallContext,
    ) -> DeviceDescriptor | SiteRef | ResourceRef:
        if resource_type in (ResourceType.DEVICE, ResourceType.SITE):
            raise self._read_them_as_objects(context.operation)
        raise self._error(
            ProviderErrorCode.MALFORMED_QUERY,
            f"NetBox does not serve resource type {resource_type}",
            context.operation,
        )

    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        """Retired. Devices are an object family and the platform projects them.

        This used to be one of two reads of the same NetBox: devices here, with each
        device's site nested inside it keyed by slug, and the site listing separately
        keyed by numeric id. Both were stored, so one NetBox site became two platform
        rows and every device attached to the one with no parent link. Keeping this as a
        fallback would have kept that second read, and with it the disagreement.
        """
        raise self._read_them_as_objects(context.operation)

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        """Retired alongside :meth:`query_devices`; see there for why."""
        raise self._read_them_as_objects(context.operation)

    def _read_them_as_objects(self, operation: str) -> InventoryProviderError:
        """The one error a provider that has not been asked to describe itself gives.

        Deliberately loud rather than quiet. The alternative -- declining to serve
        devices at all -- would let a sync succeed while quietly refreshing nothing, and
        an operator would find out when a run targeted a device whose address had
        changed weeks earlier. A failed sync that names the fix is the kinder failure.
        """
        return InventoryProviderError(
            ProviderErrorEnvelope(
                provider_id=self.id,
                provider_type=self.provider_type,
                operation=operation,
                code=ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                message=(
                    "This NetBox provider reads devices and sites as object families "
                    "and the platform writes them from the stored rows. Run Discover on "
                    "this provider once so it can say which families those are."
                ),
                retryable=False,
            )
        )

    def _fetch_plan(self, object_type: str, context: ProviderCallContext) -> _FetchPlan | None:
        """How to read ``object_type``, from the catalogue or from the platform.

        A family in the shipped catalogue is read from the catalogue, which is every
        family on a provider that has never discovered anything. A family this instance
        *discovered* is not in the catalogue -- that is what discovering it means -- and
        this provider is built fresh for the call and remembers nothing, so the platform
        hands back the mapping discovery returned, endpoint and all.

        For a catalogue family the two are **combined**, because each knows something the
        other cannot. The catalogue's paths are editorial: overlay corrections, the
        legacy spellings a replaced field keeps, the column choice. The discovered ones
        describe this instance: its custom fields, and whatever its release added to a
        family the recorded schema predates. Taking only the catalogue's would advertise
        attributes on the manifest that no read ever fills; taking only the instance's
        would let a stale discovery overwrite the plugin's own judgement. So the
        discovered paths fill in underneath and the catalogue wins where they disagree --
        the same rule the platform applies when it merges the two schemas.

        ``None`` for anything neither knows: a family nobody has ever described.
        """
        mapping = context.object_mapping
        if mapping is not None and mapping.object_type != object_type:
            # A mapping for some other family says nothing about this one.
            mapping = None
        source = self._object_sources.get(object_type)
        if source is not None:
            return _FetchPlan(
                object_type,
                source.endpoint,
                _with_discovered_paths(source.mapping, mapping),
                source.base_query,
            )
        if mapping is None:
            return None
        endpoint = mapping.source.get("endpoint", "")
        if not endpoint:
            # A mapping with nowhere to read from cannot be acted on. Falling through to
            # the SDK default reports "not supported", which is what it is.
            return None
        return _FetchPlan(object_type, endpoint, mapping, mapping.source.get("base_query", ""))

    async def list_objects(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ObjectDescriptor]:
        plan = self._fetch_plan(object_type, context)
        if plan is None:
            # device/site (and a family this provider is not configured to read) fall
            # back to the SDK default, which delegates to query_devices/list_sites or
            # raises "not supported".
            return await super().list_objects(
                object_type, query, limit=limit, cursor=cursor, context=context
            )
        items = await self._paginated(
            self._object_path(plan, _query_to_expr(query)),
            context,
            page_limit=self._services.limits.max_provider_pages,
            result_limit=limit or self._services.limits.max_run_targets,
        )
        return [
            self._with_provider_url(descriptor, item)
            for descriptor, item in zip(
                map_records(items, plan.mapping, provider_id=self.id), items, strict=True
            )
        ]

    def _object_scope(self, plan: _FetchPlan) -> str:
        """The filter this provider always applies to a family, built in and configured.

        This is the whole of what a provider is allowed to see of a family, so every read
        of it -- a list or a single object -- has to go through this. A discovered family
        is scoped the same way: ``object_queries`` is keyed by object type, and a
        discovered type has one like any other.

        ``device_query`` is folded in for devices. It scoped the device read when devices
        had a read of their own, and an operator who set it meant "this provider sees
        these devices" rather than "this particular code path sees them". Dropping it
        because the read moved would quietly widen a provider from three hundred devices
        to fifty thousand, which is the opposite of what it was set for.
        """
        configured = self.config.object_queries.get(plan.object_type, "")
        scoped = self.config.device_query if plan.object_type == "device" else ""
        return "&".join(part for part in (plan.base_query, scoped, configured) if part)

    def _object_path(self, plan: _FetchPlan, expr: str) -> str:
        """The endpoint for one family, scoped by its configured and caller filters.

        Same shape as ``device_query``: the configured baseline narrows every read of
        the family, a caller expression is appended, and NetBox ANDs the two.
        """
        parts = [part for part in (self._object_scope(plan), expr.lstrip("?")) if part]
        return f"{plan.endpoint}?{'&'.join(parts)}" if parts else plan.endpoint

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
        plan = self._fetch_plan(object_type, context)
        if plan is None:
            return await super().get_object(object_type, external_id, context=context)
        identifier = str(external_id).strip()
        if not identifier:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                "NetBox object id is empty",
                context.operation,
            )
        payload = await self._object_payload(plan, identifier, context)
        return self._with_provider_url(
            map_record(payload, plan.mapping, provider_id=self.id), payload
        )

    async def _object_payload(
        self, plan: _FetchPlan, identifier: str, context: ProviderCallContext
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
        if plan.mapping.identity != "id":
            # The row is stored under a natural key -- a site under its slug -- and
            # NetBox's detail route is keyed by its primary key, so reading the id
            # straight back would 404 on every object of that family. The list endpoint
            # takes the key as a filter, which is the same route a scoped family uses.
            return await self._single_match(
                plan, {plan.mapping.identity: identifier}, identifier, context
            )
        if not self._object_scope(plan):
            return await self._request_json(
                f"{plan.endpoint}{quote(identifier, safe='')}/", context
            )
        return await self._single_match(plan, {"id": identifier}, identifier, context)

    async def _single_match(
        self,
        plan: _FetchPlan,
        query: dict[str, str],
        identifier: str,
        context: ProviderCallContext,
    ) -> dict[str, Any]:
        """The one object matching ``query``, or NOT_FOUND.

        Going through the filtered list endpoint is what keeps a scoped provider from
        answering for an object outside its scope: the filter is applied, so an object
        another provider can see is indistinguishable from one that does not exist.
        """
        matches = await self._paginated(
            self._object_path(plan, _query_to_expr(query)),
            context,
            page_limit=self._services.limits.max_provider_pages,
            # `id` is NetBox's primary key, so at most one row can match; asking for two
            # would make the pager chase a second page that cannot hold a match.
            result_limit=1,
        )
        if not matches:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                f"NetBox {plan.object_type} {identifier} was not found within this "
                "provider's configured object query",
                context.operation,
            )
        return matches[0]

    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        """Check the connection, and report what is on the other end of it.

        ``/api/status/`` is the cheapest authenticated endpoint NetBox serves and the
        most informative: a few hundred bytes carrying the release and every installed
        plugin. Listing one device proves no more -- both need a valid token -- and
        answers nothing about *which* NetBox this is, which is the question an operator
        asks when a sync returns something they did not expect.
        """
        start = monotonic()
        status = SourceStatus()
        report: DiscoveryReport | None = None
        try:
            await self._services.validate_url(str(self.config.url), operation="test_connection")
            status = read_status(await self._request_json("/api/status/", context))
            ok = True
            error = None
            message = (
                f"NetBox {status.full_version} connection succeeded"
                if status.full_version
                else "NetBox connection succeeded"
            )
        except InventoryProviderError as exc:
            ok = False
            error = exc.envelope.to_dict()
            message = exc.envelope.message
        if ok and self.config.schema_discovery:
            report = await self._discovery_report(context)
        return ProviderTestResult(
            ok=ok,
            provider_id=self.id,
            provider_type=self.provider_type,
            latency_ms=int((monotonic() - start) * 1000),
            message=message,
            error=error,
            safe_details={**status.safe_details, **(report.safe_details if report else {})},
        )

    async def discover(self, *, context: ProviderCallContext) -> DiscoveryResult:
        """Describe what this NetBox serves, from its own OpenAPI document.

        This is the answer the shipped catalogue cannot give. It is written against
        NetBox in general; an instance runs one release, with its own plugins installed
        and its own custom fields defined, and only it can say which object types those
        add up to.

        Families the catalogue already knows keep the ids this plugin registers, so
        discovery extends what a provider offers instead of shadowing it -- see
        :func:`~.derive.registered_catalogue`. Devices and sites are in there like any
        other family, with a binding saying which is which, because the platform writes
        those two tables from the rows this provider stored rather than reading them a
        second time.

        Unlike the connection test's best-effort probe, a failure here is raised: the
        caller asked for a description and there is none to give.
        """
        derived, _custom_fields = await self._fetch_derived(context)
        catalogue = registered_catalogue(derived)
        # Which of those families the platform's devices and sites are written from, and
        # how. Built from this provider's own config rather than from the shipped
        # defaults: which hierarchy becomes the site tree, which NetBox status means "in
        # maintenance" and which custom field holds a credential reference are choices an
        # operator makes per provider, and the binding is now the only thing that carries
        # them. Sent only for families this instance actually serves, so an instance
        # whose regions were trimmed gets a flat site list rather than a binding that
        # projects from rows which never arrive.
        composed = bindings_for(self.config, served={entry.endpoint for entry in catalogue})
        unmappable = unsupported_field_map_keys(self.config)
        return DiscoveryResult(
            source_version=derived.source_version,
            object_types=tuple(entry.spec for entry in catalogue),
            mappings={entry.id: entry.mapping for entry in catalogue},
            bindings=tuple(
                composed[entry.endpoint] for entry in catalogue if entry.endpoint in composed
            ),
            # What the deriver could not interpret, plus any field this provider is
            # configured to remap that a binding cannot express. Both are shapes the
            # provider cannot honour, which is what this field is for -- and a silently
            # ignored credential mapping is exactly the kind of thing an operator needs
            # told rather than left to discover from a failed run.
            warnings=(
                *derived.warnings,
                *(
                    f"field_map.{key} is set but projection cannot honour it; "
                    "the stored object fixes this attribute"
                    for key in unmappable
                ),
            ),
        )

    async def _fetch_derived(
        self, context: ProviderCallContext
    ) -> tuple[DerivedCatalogue, builtins.list[dict[str, Any]]]:
        """Read this instance's schema and custom fields, and derive a catalogue."""
        # NetBox negotiates on Accept and defaults to YAML; drf-spectacular takes the
        # format explicitly.
        schema = await self._request_json("/api/schema/?format=json", context)
        custom_fields = await self._paginated(
            "/api/extras/custom-fields/",
            context,
            page_limit=self._services.limits.max_provider_pages,
            # No preview cap. A custom field this read drops is an attribute the
            # discovered catalogue never mentions, on an instance that defines it -- and
            # nothing downstream could tell that from an instance that does not. The
            # page limit still bounds the read, and it fails loudly.
            result_limit=None,
        )
        return derive_catalogue(schema, custom_fields=custom_fields), custom_fields

    async def _discovery_report(self, context: ProviderCallContext) -> DiscoveryReport | None:
        """The connection test's summary of the same description.

        Deliberately best-effort, unlike :meth:`discover`. The connection this reports on
        already succeeded, so a schema that cannot be fetched or parsed -- an older
        release, a proxy that strips the endpoint, a NetBox large enough to time out --
        must leave that result alone rather than turn a working provider into a failing
        one. What is lost is the extra detail, which is why there is nothing to say when
        it fails.
        """
        try:
            derived, custom_fields = await self._fetch_derived(context)
        except InventoryProviderError:
            return None
        except (ValueError, TypeError, KeyError, AttributeError):
            # A document shaped like nothing the deriver knows. Same trade as above.
            return None
        return compare_to_catalogue(derived, custom_fields=len(custom_fields))

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
        self, path: str, context: ProviderCallContext, *, page_limit: int, result_limit: int | None
    ) -> builtins.list[dict[str, Any]]:
        """Every record behind ``path``, following NetBox's ``next`` links.

        ``result_limit`` is a preview cap: it truncates, silently, because a preview of
        the first N devices is the whole point of asking for one. ``None`` says this
        caller needs the *complete* listing and would be wrong to work from part of it.
        Such a read is still bounded -- by ``page_limit``, which raises rather than
        returning a short answer that looks like the full one.
        """
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
            if result_limit is not None and len(items) >= result_limit:
                return items[:result_limit]
            next_raw = page.get("next") if isinstance(page, dict) else None
            next_url = next_raw if isinstance(next_raw, str) and next_raw else None
        return items

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
