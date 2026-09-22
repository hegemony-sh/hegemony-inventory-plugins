# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast
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
    ProjectionBinding,
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

from .bindings import bindings_for
from .config import NetBoxProviderConfig
from .derive import (
    DerivedCatalogue,
    DiscoveryReport,
    SourceStatus,
    compare_to_catalogue,
    derive_catalogue,
    read_status,
    registered_catalogue,
)
from .filters import LOOKUP_SUFFIX as _SCOPE_LOOKUP_SUFFIX, intersect_queries as _intersect_queries
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
    query_schema: Mapping[str, Any] = field(default_factory=dict)


class NetBoxInventoryProvider(InventoryProvider):
    provider_type = "netbox"
    capabilities = frozenset(
        {Capability.READ, Capability.QUERY, Capability.DISCOVER, Capability.DELTA}
    )
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

    @property
    def discovery_cache_key(self) -> str:
        url = urlparse(str(self.config.discovery_url or self.config.url))
        scheme, host, port = self._origin(url)
        identity = f"{scheme}://{host.lower()}:{port}{url.path.rstrip('/')}"
        # Version the derivation contract as well as the instance identity.
        return hashlib.sha256(f"netbox-schema-v2:{identity}".encode()).hexdigest()

    def configured_projection_bindings(
        self, object_types: Iterable[str]
    ) -> tuple[ProjectionBinding, ...]:
        """Rebuild discovered bindings from the provider's current configuration.

        Discovery proves which families this instance serves. Settings such as the site
        tree, status map, credential fields, and tag prefix remain ordinary mutable
        provider configuration and must take effect on the next sync rather than being
        frozen into the discovery snapshot.
        """
        served = {
            source.endpoint
            for object_type in object_types
            if (source := self._object_sources.get(object_type)) is not None
        }
        composed = bindings_for(self.config, served=served)
        return tuple(composed[endpoint] for endpoint in sorted(served) if endpoint in composed)

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
        discovered_query_schema = context.object_query_schema
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
                {
                    "type": "object",
                    "properties": {
                        **dict(source.query_schema.get("properties") or {}),
                        **dict(discovered_query_schema.get("properties") or {}),
                    },
                },
            )
        if mapping is None:
            return None
        endpoint = mapping.source.get("endpoint", "")
        if not endpoint:
            # A mapping with nowhere to read from cannot be acted on. Falling through to
            # the SDK default reports "not supported", which is what it is.
            return None
        return _FetchPlan(
            object_type,
            endpoint,
            mapping,
            mapping.source.get("base_query", ""),
            discovered_query_schema,
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
        plan = self._fetch_plan(object_type, context)
        if plan is None:
            # device/site (and a family this provider is not configured to read) fall
            # back to the SDK default, which delegates to query_devices/list_sites or
            # raises "not supported".
            return await super().list_objects(
                object_type, query, limit=limit, cursor=cursor, context=context
            )
        if not self._tenant_scope_allows(plan):
            return []
        with self._query_errors(context):
            path = self._object_path(plan, self._with_since(_query_to_expr(query), context))
        items = await self._paginated(
            path,
            context,
            page_limit=self._services.limits.max_provider_pages,
            # None means a complete synchronization listing. Preview callers pass their
            # own explicit cap; max_run_targets limits one run, not inventory size.
            result_limit=limit,
        )
        return [
            self._with_marker(self._with_provider_url(descriptor, item), item)
            for descriptor, item in zip(
                map_records(
                    (self._assignment_record(item, plan.object_type) for item in items),
                    plan.mapping,
                    provider_id=self.id,
                ),
                items,
                strict=True,
            )
        ]

    async def iter_object_pages(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        context: ProviderCallContext,
    ) -> AsyncIterator[builtins.list[ObjectDescriptor]]:
        plan = self._fetch_plan(object_type, context)
        if plan is None:
            yield await super().list_objects(object_type, query, context=context)
            return
        if not self._tenant_scope_allows(plan):
            # A complete empty page lets the platform stale rows from an earlier,
            # wider configuration instead of leaving them active after isolation is
            # enabled. Config edits already force this first pass to be a full read.
            yield []
            return
        with self._query_errors(context):
            path = self._object_path(plan, self._with_since(_query_to_expr(query), context))
        async for items in self._iter_paginated(
            path,
            context,
            page_limit=self._services.limits.max_provider_pages,
        ):
            yield [
                self._with_marker(self._with_provider_url(descriptor, item), item)
                for descriptor, item in zip(
                    map_records(
                        (self._assignment_record(item, plan.object_type) for item in items),
                        plan.mapping,
                        provider_id=self.id,
                    ),
                    items,
                    strict=True,
                )
            ]

    def _with_since(self, expr: str, context: ProviderCallContext) -> str:
        """Narrow a read to what changed, when the platform handed back a marker.

        NetBox stamps ``last_updated`` on every object and filters on it, so the whole
        of delta reading here is one more query parameter. ``__gte`` rather than ``__gt``
        deliberately: two objects saved in the same instant carry the same timestamp, and
        excluding the boundary would drop whichever of them the previous page did not
        reach. Re-reading one object costs an upsert that changes nothing.
        """
        if not context.since:
            return expr
        clause = f"last_updated__gte={quote(self._since_with_grace(context.since), safe='')}"
        return f"{expr}&{clause}" if expr else clause

    def _since_with_grace(self, marker: str) -> str:
        """The marker moved back by ``delta_grace_seconds``, in NetBox's own spelling.

        ``last_updated`` is stamped when a save begins, not when its transaction
        commits, so a long transaction can carry a stamp older than the marker the
        previous pass recorded and still be invisible to that pass. Re-reading the tail
        of the previous pass costs a few upserts that change nothing and catches those.
        The marker the platform records stays the true highest value seen; only the
        boundary the next read asks for moves. A marker this provider cannot parse --
        it is opaque to the platform by contract -- is handed back as it was rather than
        dropped, which would turn a delta into a full read without saying so.
        """
        grace = self.config.delta_grace_seconds
        if not grace:
            return marker
        try:
            stamp = datetime.fromisoformat(marker)
        except ValueError:
            return marker
        earlier = stamp - timedelta(seconds=grace)
        if stamp.tzinfo is not None and marker.endswith("Z"):
            return earlier.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return earlier.isoformat()

    @staticmethod
    def _with_marker(descriptor: ObjectDescriptor, item: Mapping[str, Any]) -> ObjectDescriptor:
        """Stamp where this record stood in NetBox's ordering, for the next delta read.

        Without it the platform has nothing to carry back and every sync is a full one,
        which is the safe failure rather than the silent one: a provider that filtered by
        ``since`` and then reported no marker would leave the platform asking for changes
        since a moment that never advanced.
        """
        marker = item.get("last_updated")
        if not isinstance(marker, str) or not marker:
            return descriptor
        return replace(descriptor, source_version={**descriptor.source_version, "marker": marker})

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
        return _intersect_queries(plan.base_query, self._saved_scope_query(plan))

    def _saved_scope_query(self, plan: _FetchPlan) -> str:
        """Compile the active named scope into NetBox's filter query language."""
        scope = self.config.inventory_filter
        if scope is None:
            return ""

        pairs: list[tuple[str, str]] = []
        tenant_filter = self._tenant_scope_filter(plan)
        if tenant_filter is not None:
            pairs.append(tenant_filter)

        query = urlencode(pairs)
        advertised = set((plan.query_schema or {}).get("properties") or {})
        for clause in scope.queries.get(plan.object_type, ()):
            parameter = f"{clause.field}{_SCOPE_LOOKUP_SUFFIX[clause.lookup]}"
            if clause.schema_validation == "required" and parameter not in advertised:
                raise ValueError(f"NetBox {plan.object_type} does not advertise filter {parameter}")
            query = _intersect_queries(query, urlencode({parameter: clause.value}, doseq=True))
        return query

    def _tenant_scope_filter(self, plan: _FetchPlan) -> tuple[str, str] | None:
        """The tenant filter this instance proved the family accepts, if any."""
        scope = self.config.inventory_filter
        if scope is None or not scope.tenant_slug:
            return None
        query_fields = set((plan.query_schema or {}).get("properties") or {})
        if plan.object_type == "tenant" and "slug" in query_fields:
            return ("slug", scope.tenant_slug)
        if "tenant" in plan.mapping.field_map and "tenant" in query_fields:
            return ("tenant", scope.tenant_slug)
        return None

    def _tenant_scope_allows(self, plan: _FetchPlan) -> bool:
        """Fail closed when a tenant scope cannot constrain this family.

        Additional rules may narrow a family but do not prove tenant ownership. An
        exemption is therefore explicit: it is for shared reference data or a NetBox
        account whose object permissions enforce the boundary independently.
        """
        scope = self.config.inventory_filter
        if scope is None or not scope.tenant_slug:
            return True
        return (
            self._tenant_scope_filter(plan) is not None
            or plan.object_type in scope.tenant_scope_exemptions
        )

    @contextmanager
    def _query_errors(self, context: ProviderCallContext) -> Iterator[None]:
        """Expose invalid scope declarations through the provider error contract."""
        try:
            yield
        except ValueError as exc:
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY, str(exc), context.operation
            ) from exc

    def _object_path(self, plan: _FetchPlan, expr: str) -> str:
        """The endpoint for one family, scoped by its configured and caller filters.

        Both configured and caller filters must hold, including repeated parameters.
        """
        query = _intersect_queries(self._object_scope(plan), expr)
        return f"{plan.endpoint}?{query}" if query else plan.endpoint

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
        if not self._tenant_scope_allows(plan):
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                f"NetBox {object_type} is suppressed because the active tenant scope "
                "cannot constrain that family",
                context.operation,
            )
        identifier = str(external_id).strip()
        if not identifier:
            raise self._error(
                ProviderErrorCode.NOT_FOUND,
                "NetBox object id is empty",
                context.operation,
            )
        payload = await self._object_payload(plan, identifier, context)
        return self._with_provider_url(
            map_record(
                self._assignment_record(payload, plan.object_type),
                plan.mapping,
                provider_id=self.id,
            ),
            payload,
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
        with self._query_errors(context):
            scope = self._object_scope(plan)
        if not scope:
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
        with self._query_errors(context):
            path = self._object_path(plan, _query_to_expr(query))
        matches = await self._paginated(
            path,
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
        return DiscoveryResult(
            source_version=derived.source_version,
            object_types=tuple(entry.spec for entry in catalogue),
            mappings={entry.id: entry.mapping for entry in catalogue},
            bindings=tuple(
                composed[entry.endpoint] for entry in catalogue if entry.endpoint in composed
            ),
            warnings=derived.warnings,
        )

    async def _fetch_derived(
        self, context: ProviderCallContext
    ) -> tuple[DerivedCatalogue, builtins.list[dict[str, Any]]]:
        """Read this instance's schema and custom fields, and derive a catalogue."""
        # NetBox negotiates on Accept and defaults to YAML; drf-spectacular takes the
        # format explicitly.
        discovery_url = str(self.config.discovery_url or self.config.url)
        cache = getattr(self._services, "schema_cache", None)
        schema = await cache.get(self.discovery_cache_key) if cache is not None else None
        refresh = schema is None or context.operation == "discover"
        if refresh:
            schema = await self._request_json(
                "/api/schema/?format=json", context, base_url=discovery_url
            )
        else:
            # Cached structure is not proof this provider can access the source.
            # OPTIONS runs the endpoint's permission checks without generating the
            # OpenAPI document again. Object reads retain their own authorization.
            # OPTIONS uses the same content negotiation as GET, so request JSON
            # explicitly here too instead of trying to parse the default YAML.
            await self._request_json(
                "/api/schema/?format=json", context, method="OPTIONS", base_url=discovery_url
            )
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
        derived = derive_catalogue(schema, custom_fields=custom_fields)
        if refresh and cache is not None:
            await cache.put(self.discovery_cache_key, schema)
        return derived, custom_fields

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

    async def _request_json(
        self,
        path_or_url: str,
        context: ProviderCallContext,
        *,
        client: httpx.AsyncClient | None = None,
        method: str = "GET",
        base_url: str | None = None,
    ) -> dict[str, Any]:
        base_url = base_url or str(self.config.url)
        await self._services.validate_url(base_url, operation=context.operation)
        url = self._request_url(path_or_url, context.operation, base_url=base_url)
        token = await self._services.resolve_secret_ref(
            self.config.token_ref, operation=context.operation
        )
        authorization_values = self._authorization_header_values(token)
        retry_attempts = 4
        async with AsyncExitStack() as stack:
            if client is None:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        timeout=self.config.timeout_seconds,
                        verify=self.config.verify_tls,
                        follow_redirects=False,
                    )
                )
            for attempt in range(retry_attempts):
                for auth_index, authorization in enumerate(authorization_values):
                    headers = {"Authorization": authorization} if authorization else {}
                    try:
                        # Streamed rather than fetched whole: the status arrives before
                        # the body does, so an error response is never read at all, and
                        # a success is read under the cap in _read_capped.
                        async with client.stream(method, url, headers=headers) as resp:
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
                            body = await self._read_capped(resp, context.operation)
                        try:
                            data = json.loads(body)
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

    def _request_url(self, path_or_url: str, operation: str, *, base_url: str | None = None) -> str:
        base_url = base_url or str(self.config.url)
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

    async def _read_capped(self, response: httpx.Response, operation: str) -> bytes:
        """Read a streamed body, refusing one that grows past the configured cap.

        A timeout bounds how long a read may take. Nothing bounded how much it may
        return, and the two are not the same failure: a source that answers steadily and
        forever never trips a timeout, and the body is buffered in the API process
        before anything parses it. So the read stops at a byte count rather than only at
        a clock.

        The declared ``Content-Length`` is checked first where there is one, so an
        oversized response costs nothing to refuse, but it is only an optimisation --
        the header is the sender's claim about itself. The running total is what
        actually decides, which is what makes a chunked response with no length, or one
        whose length lies, no different from any other.
        """
        cap = self.config.max_response_bytes
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    raise self._too_large(operation, cap)
            except ValueError:
                # An unparsable length decides nothing; the running total still does.
                pass
        chunks: builtins.list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                # Abandoned unparsed, and the `async with` closes the connection on the
                # way out rather than draining what is left of the body.
                raise self._too_large(operation, cap)
            chunks.append(chunk)
        return b"".join(chunks)

    def _too_large(self, operation: str, cap: int) -> InventoryProviderError:
        """The one error a capped read raises; not retryable, because a retry reads it again."""
        return self._error(
            ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
            f"NetBox response exceeded the {cap}-byte limit for this provider",
            operation,
            retryable=False,
            safe_details={"max_response_bytes": cap},
        )

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

    def _page_sized(self, path: str) -> str:
        """The first page of ``path``: sized by ``page_size`` and ordered by ``id``.

        NetBox pages by offset and orders by name unless told otherwise, and a name
        order is stable only for an instant. A record inserted while a listing is in
        progress lands wherever its name sorts; if that is before the offset already
        read, every later record shifts by one, the boundary record arrives twice and
        the platform fails the family on the duplicate. Ordered by ``id`` a concurrent
        insert can only land on the last page, because NetBox ids only grow. A deletion
        before the offset -- or an edit that takes a record out of the filter -- still
        shifts one record off the listing; the periodic full pass is what corrects that,
        and the README says so. A caller's own ``limit`` or ``ordering`` is respected.
        """
        present = {part.partition("=")[0] for part in urlparse(path).query.split("&")}
        additions = "&".join(
            f"{key}={value}"
            for key, value in (("limit", self.config.page_size), ("ordering", "id"))
            if key not in present
        )
        if not additions:
            return path
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}{additions}"

    async def _iter_paginated(
        self,
        path: str,
        context: ProviderCallContext,
        *,
        page_limit: int,
    ) -> AsyncIterator[builtins.list[dict[str, Any]]]:
        next_url: str | None = self._page_sized(path)
        pages = 0
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_tls,
            follow_redirects=False,
        ) as client:
            while next_url:
                pages += 1
                if pages > page_limit:
                    raise self._error(
                        ProviderErrorCode.PAGINATION_FAILED,
                        "NetBox pagination limit exceeded",
                        context.operation,
                        safe_details={"page": pages},
                    )
                page = await self._request_json(next_url, context, client=client)
                raw_results = page.get("results")
                if not isinstance(raw_results, list):
                    raise self._error(
                        ProviderErrorCode.PAGINATION_FAILED,
                        "NetBox pagination response is malformed",
                        context.operation,
                        safe_details={"page": pages},
                    )
                records: builtins.list[dict[str, Any]] = []
                for index, item in enumerate(raw_results):
                    if not isinstance(item, dict):
                        raise self._error(
                            ProviderErrorCode.PAGINATION_FAILED,
                            "NetBox pagination result is not an object",
                            context.operation,
                            safe_details={"page": pages, "result_index": index},
                        )
                    records.append(cast(dict[str, Any], item))
                yield records
                next_raw = page.get("next")
                if next_raw is not None and not isinstance(next_raw, str):
                    raise self._error(
                        ProviderErrorCode.PAGINATION_FAILED,
                        "NetBox pagination next link is malformed",
                        context.operation,
                        safe_details={"page": pages},
                    )
                next_url = next_raw if isinstance(next_raw, str) and next_raw else None

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
        async for records in self._iter_paginated(path, context, page_limit=page_limit):
            items.extend(records)
            if result_limit is not None and len(items) >= result_limit:
                return items[:result_limit]
        return items

    @staticmethod
    def _assignment_record(item: Mapping[str, Any], object_type: str) -> Mapping[str, Any]:
        if object_type != "ip_address":
            return item
        target = {"dcim.interface": "interface", "virtualization.vminterface": "vm_interface"}.get(
            str(item.get("assigned_object_type"))
        )
        # Synthetic keys cannot be supplied by an upstream record for the other type.
        result = {**item, "_hegemony_interface": None, "_hegemony_vm_interface": None}
        assigned = item.get("assigned_object")
        if target and isinstance(assigned, Mapping):
            result[f"_hegemony_{target}"] = {"id": assigned.get("id")}
        return result

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
            public = urlparse(str(self.config.public_url or self.config.url))
            base = urlparse(str(self.config.url)).path.rstrip("/")
            if base and path.startswith(base + "/"):
                path = path[len(base) :]
            return f"{public.scheme}://{public.netloc}{public.path.rstrip('/')}/{path.lstrip('/')}"
        return None

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
