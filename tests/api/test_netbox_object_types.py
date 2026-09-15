# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The NetBox object-type catalogue: what it declares, and what a provider reads.

The plugin can read every NetBox family in ``object_types.OBJECT_TYPES``; a provider
reads the ones its ``object_types`` config names. These tests pin both halves, and the
consistency of the catalogue itself -- it is data, and data with a typo in it fails at
sync time against a real NetBox rather than here, which is the wrong place to find out.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from hegemony_inventory_netbox import (
    object_types as catalogue,
    provider as netbox_provider_module,
    register,
)
from hegemony_inventory_netbox.config import NetBoxProviderConfig
from hegemony_inventory_netbox.overlay import EXTERNAL_ID_KEYS
from hegemony_inventory_netbox.provider import NetBoxInventoryProvider
from hegemony_inventory_sdk import (
    InventoryProviderError,
    ObjectTypeSpec,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderFactory,
    ResourceType,
    map_record,
)
from tests.inventory_fakes import FakePlatformServices

_CORE_RESOURCES = {ResourceType.DEVICE.value, ResourceType.SITE.value}
_ENDPOINT = re.compile(r"^/api/[a-z][a-z0-9-]*/[a-z][a-z0-9-]*/$")
#: An object-type id is either a bare singular -- the fifty this plugin promised before
#: the deriver existed and must keep offering -- or ``app.family``, which the deriver
#: reads straight off the endpoint and so can produce for a family nobody wrote down.
_OBJECT_TYPE_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")


def _provider(config: dict[str, Any] | None = None) -> NetBoxInventoryProvider:
    return NetBoxInventoryProvider(
        provider_id="netbox:primary",
        config={
            "url": "https://netbox.example",
            "token_ref": "{{ secret('vault://inventory/netbox/token') }}",
            **(config or {}),
        },
        services=FakePlatformServices(
            token="netbox-token", provider_id="netbox:primary", provider_type="netbox"
        ),
    )


def _context(operation: str) -> ProviderCallContext:
    return ProviderCallContext(
        provider_id="netbox:primary",
        operation=operation,
        query_hash="",
        config_version=1,
    )


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(netbox_provider_module.httpx, "AsyncClient", client_factory)


def _object_types(provider: NetBoxInventoryProvider) -> set[str]:
    return {
        resource.value if isinstance(resource, ResourceType) else str(resource)
        for resource in provider.supported_resources
    }


class _RecordingRegistry:
    """Captures what ``register()`` declares, without a platform behind it."""

    api_version = 1

    def __init__(self) -> None:
        self.object_types: list[ObjectTypeSpec] = []
        self.provider_types: list[dict[str, Any]] = []

    def register_provider_type(
        self,
        *,
        provider_type: str,
        display_name: str,
        description: str,
        capabilities: list[str],
        supported_resources: list[str],
        factory: ProviderFactory,
        config_model: type[BaseModel] | None = None,
        config_schema: dict[str, Any] | None = None,
        default_config: dict[str, Any] | None = None,
    ) -> None:
        self.provider_types.append(
            {
                "provider_type": provider_type,
                "supported_resources": supported_resources,
                "default_config": default_config or {},
            }
        )

    def register_object_type(self, spec: ObjectTypeSpec) -> None:
        self.object_types.append(spec)


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_every_catalogue_entry_is_well_formed(entry: catalogue.NetBoxObjectType) -> None:
    """One entry, checked the way a reader would check it before trusting it."""
    assert _OBJECT_TYPE_ID.match(entry.id), entry.id
    if entry.id in _CORE_RESOURCES:
        # Devices and sites are families like any other now, and that is only safe
        # because they carry a projection binding: it is what says the stored rows
        # become the platform's devices and sites tables rather than a second, parallel
        # copy of them under a generic object type.
        assert entry.binding is not None, (
            f"{entry.id} is in the catalogue without a binding, so its rows would be "
            "stored as generic objects and never reach the table they belong in"
        )
    assert _ENDPOINT.match(entry.endpoint), entry.endpoint
    # A family is stored under one key and cited by the same one. NetBox's numeric
    # primary key is what every family has and what every reference carries, so it is
    # the default; a family that overrides it must say so in the overlay, and both
    # spellings then come from there. Storing a row under one key while every edge into
    # it cites another is the shape of the defect all of this exists to remove.
    assert entry.identity == EXTERNAL_ID_KEYS.get(entry.endpoint, "id"), entry.id
    assert entry.identity_keys == (entry.identity,)
    assert entry.display_name and entry.plural and entry.description
    assert entry.attributes, "a family with no attributes stores nothing worth listing"

    paths = {attr.path for attr in entry.attributes.values()}
    assert entry.name_field == "display" or entry.name_field in paths, entry.name_field
    # Columns and search fields name stored attributes; "name" is the descriptor's own.
    for column in entry.columns:
        assert column == "name" or column in entry.attributes, f"{entry.id}: {column}"
    for search_field in entry.search_fields:
        assert search_field == "name" or search_field in entry.attributes, (
            f"{entry.id}: {search_field}"
        )
    for relationship in entry.relationships:
        assert relationship.source, f"{entry.id}: relationship without a source path"
        assert (
            relationship.target_object_type in catalogue.BY_ID
            or relationship.target_object_type in _CORE_RESOURCES
        ), f"{entry.id}: {relationship.target_object_type} is served by nothing"

    spec = entry.spec
    assert spec.id == entry.id
    assert set(spec.field_schema["properties"]) == set(entry.attributes)
    assert {r.rel for r in spec.relationships} == {r.rel for r in entry.relationships}
    mapping = entry.mapping
    assert mapping.object_type == entry.id
    assert set(mapping.field_map) == set(entry.attributes)


def test_catalogue_ids_are_unique() -> None:
    ids = [entry.id for entry in catalogue.OBJECT_TYPES]
    assert len(ids) == len(set(ids)), "duplicate ids would collide in the platform registry"
    assert tuple(sorted(ids)) == catalogue.ALL_OBJECT_TYPE_IDS


def test_every_family_is_grouped_under_its_netbox_app() -> None:
    """Fifty flat checkboxes are not a choice; the platform groups them by this."""
    for entry in catalogue.OBJECT_TYPES:
        assert entry.spec.group == catalogue.APP_GROUPS[entry.app], entry.id
    assert {entry.spec.group for entry in catalogue.OBJECT_TYPES} == set(
        catalogue.APP_GROUPS.values()
    )


def test_the_readme_documents_every_catalogue_family() -> None:
    """The catalogue table in the README is a promise; drift makes it a lie."""
    readme = (
        pathlib.Path(__file__).resolve().parents[2] / "plugins" / "inventory_netbox" / "README.md"
    ).read_text(encoding="utf-8")
    undocumented = [entry.id for entry in catalogue.OBJECT_TYPES if f"`{entry.id}`" not in readme]
    assert not undocumented, f"README does not list: {', '.join(undocumented)}"
    assert f"{len(catalogue.OBJECT_TYPES)} families" in readme


def test_register_declares_every_catalogue_family() -> None:
    registry = _RecordingRegistry()
    register(registry)

    assert [spec.id for spec in registry.object_types] == [
        entry.id for entry in catalogue.OBJECT_TYPES
    ]
    (provider_type,) = registry.provider_types
    assert provider_type["provider_type"] == "netbox"
    # The type declares the whole space so an operator has something to choose from.
    assert set(provider_type["supported_resources"]) == _CORE_RESOURCES | set(
        catalogue.ALL_OBJECT_TYPE_IDS
    )
    # The form must not pin a narrower ceiling into a new provider's config: that is
    # what made a ticked object type sync nothing at all.
    assert "object_types" not in provider_type["default_config"]


def test_a_provider_offers_every_family_whatever_its_config_says() -> None:
    """One control decides what syncs, and it is not this one.

    A config that could also narrow the space is exactly what made a ticked object
    type sync nothing: the platform can only subtract from what a provider declares.
    A stored ``object_types`` from when this plugin had that setting is ignored rather
    than rejected, so a provider configured then still validates.
    """
    every = _CORE_RESOURCES | set(catalogue.ALL_OBJECT_TYPE_IDS)
    assert _object_types(_provider()) == every
    assert _object_types(_provider({"object_types": ["interface", "rack"]})) == every
    assert _object_types(_provider({"object_types": []})) == every
    assert "object_types" not in _provider({"object_types": ["rack"]}).config.model_dump()


@pytest.mark.asyncio
async def test_a_family_the_catalogue_does_not_know_is_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id nothing catalogues is refused, not silently answered with nothing.

    The platform only asks for types the provider declares, so this bites a direct
    caller -- and for that caller "not supported" is the honest answer.
    """
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, handler)
    provider = _provider()

    with pytest.raises(InventoryProviderError) as excinfo:
        await provider.list_objects("unicorn", context=_context("list_objects:unicorn"))
    assert "not supported" in excinfo.value.envelope.message
    with pytest.raises(InventoryProviderError):
        await provider.get_object("unicorn", "1", context=_context("get_object:unicorn"))
    assert requested == [], "an uncatalogued family must not reach NetBox at all"


_INTERFACE_PAYLOAD = {
    "id": 41,
    "url": "https://netbox.example/api/dcim/interfaces/41/",
    "display": "ge-0/0/1",
    "device": {"id": 7, "name": "ams01-core-01"},
    "name": "ge-0/0/1",
    "type": {"value": "1000base-t", "label": "1000BASE-T"},
    "enabled": True,
    "mtu": 9000,
    "mac_address": "AA:BB:CC:DD:EE:01",
    "mode": {"value": "tagged"},
    "lag": {"id": 40, "name": "ae0"},
    "untagged_vlan": {"id": 12, "vid": 100},
    "mark_connected": False,
    "description": "uplink",
}


@pytest.mark.asyncio
async def test_list_objects_maps_a_catalogue_family_with_its_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interface, read and mapped: attributes, name, and the graph around it."""
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": [_INTERFACE_PAYLOAD], "next": None})

    _install_transport(monkeypatch, handler)
    provider = _provider({"object_types": ["interface"]})

    objects = await provider.list_objects("interface", context=_context("list_objects:interface"))

    assert paths == ["/api/dcim/interfaces/"]
    (interface,) = objects
    assert interface.object_type == "interface"
    assert interface.external_id == "41"
    assert interface.name == "ge-0/0/1"
    # The mapper emits exactly what the family declares -- no more, so a record cannot
    # smuggle a field past the catalogue, and no fewer, so a path this record does not
    # carry is stored as null rather than quietly missing.
    assert set(interface.attributes) == set(catalogue.BY_ID["interface"].attributes)
    expected = {
        "name": "ge-0/0/1",
        "device": "ams01-core-01",
        "type": "1000base-t",
        "enabled": True,
        "mtu": 9000,
        "mac_address": "AA:BB:CC:DD:EE:01",
        "primary_mac_address": None,
        "mode": "tagged",
        "lag": "ae0",
        "untagged_vlan": 100,
    }
    assert {name: interface.attributes[name] for name in expected} == expected
    # Edges cite the same external ids the related rows are keyed on, so an interface
    # points at the device row a device sync created.
    assert {
        (r.rel, r.target_object_type, r.target_external_id) for r in interface.relationships
    } == {
        ("device", "device", "7"),
        ("lag", "interface", "40"),
        ("untagged_vlan", "vlan", "12"),
    }
    # "Open in NetBox" points at the UI page, not at the API resource it came from.
    assert interface.descriptive["provider_url"] == ("https://netbox.example/dcim/interfaces/41/")


@pytest.mark.asyncio
async def test_a_configured_query_scopes_a_family_and_a_caller_query_narrows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``object_queries`` is to a family what ``device_query`` is to devices."""
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, handler)
    provider = _provider(
        {
            "object_types": ["interface", "ip_prefix"],
            "object_queries": {"interface": "?mgmt_only=false", "ip_prefix": "status=active"},
        }
    )

    await provider.list_objects("interface", context=_context("list_objects:interface"))
    await provider.list_objects(
        "interface", {"device_id": 7}, context=_context("list_objects:interface")
    )
    await provider.list_objects("ip_prefix", context=_context("list_objects:ip_prefix"))

    assert urls == [
        "https://netbox.example/api/dcim/interfaces/?mgmt_only=false",
        "https://netbox.example/api/dcim/interfaces/?mgmt_only=false&device_id=7",
        "https://netbox.example/api/ipam/prefixes/?status=active",
    ]


@pytest.mark.asyncio
async def test_get_object_reads_one_object_of_a_catalogue_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_INTERFACE_PAYLOAD)

    _install_transport(monkeypatch, handler)
    provider = _provider()

    interface = await provider.get_object("interface", "41", context=_context("get_object"))

    assert paths == ["/api/dcim/interfaces/41/"]
    assert interface.external_id == "41"
    assert interface.attributes["device"] == "ams01-core-01"


@pytest.mark.asyncio
async def test_get_object_cannot_escape_the_family_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped family answers for the objects inside its scope and no others.

    NetBox's detail route takes no filters, so reading it directly would let any caller
    that knows an id pull an object a provider was deliberately narrowed away from. The
    id is resolved through the filtered list endpoint instead, which makes an object
    outside the scope indistinguishable from one that does not exist.
    """
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        # NetBox answers a filtered list; interface 41 is on device 7, interface 99 is not.
        in_scope = request.url.params.get("id") == "41"
        return httpx.Response(
            200, json={"results": [_INTERFACE_PAYLOAD] if in_scope else [], "next": None}
        )

    _install_transport(monkeypatch, handler)
    provider = _provider(
        {"object_types": ["interface"], "object_queries": {"interface": "device_id=7"}}
    )

    interface = await provider.get_object("interface", "41", context=_context("get_object"))
    assert interface.external_id == "41"

    with pytest.raises(InventoryProviderError) as excinfo:
        await provider.get_object("interface", "99", context=_context("get_object"))
    assert excinfo.value.envelope.code == ProviderErrorCode.NOT_FOUND.value

    # Both reads went to the filtered list endpoint, never to /interfaces/<id>/.
    assert urls == [
        "https://netbox.example/api/dcim/interfaces/?device_id=7&id=41",
        "https://netbox.example/api/dcim/interfaces/?device_id=7&id=99",
    ]


# NetBox 4.2 replaced Cluster.site and CircuitTermination.site/provider_network with
# generic scopes, and 4.3 did the same to Service.device/virtual_machine. A path that is
# absent maps to None, so mapping only the old spelling silently yields nothing at all on
# a current NetBox -- which is why both are mapped and why this pins each shape.
_GENERIC_RELATION_CASES = [
    pytest.param(
        "cluster",
        {
            "id": 3,
            "display": "ams-cluster",
            "name": "ams-cluster",
            "scope_type": "dcim.site",
            "scope": {"id": 5, "name": "AMS 01", "slug": "ams01"},
        },
        {"scope": "AMS 01", "scope_type": "dcim.site", "site": None},
        id="cluster-scope",
    ),
    pytest.param(
        "circuit_termination",
        {
            "id": 8,
            "display": "Termination A",
            "term_side": "A",
            "circuit": {"id": 2, "cid": "ntt-1"},
            "termination_type": "circuits.providernetwork",
            "termination": {"id": 9, "name": "NTT Global"},
        },
        {
            "termination": "NTT Global",
            "termination_type": "circuits.providernetwork",
            "site": None,
            "provider_network": None,
        },
        id="circuit-termination",
    ),
    pytest.param(
        "service",
        {
            "id": 11,
            "display": "ssh",
            "name": "ssh",
            "ports": [22],
            "parent_object_type": "ipam.fhrpgroup",
            "parent": {"id": 4, "display": "VRRP2 group 10"},
        },
        # An FHRP group has no name, which is why `display` is read.
        {
            "parent": "VRRP2 group 10",
            "parent_object_type": "ipam.fhrpgroup",
            "device": None,
            "virtual_machine": None,
        },
        id="service-parent",
    ),
]


@pytest.mark.parametrize(("object_type", "payload", "expected"), _GENERIC_RELATION_CASES)
def test_the_generic_scopes_netbox_moved_to_are_mapped(
    object_type: str, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    descriptor = map_record(
        payload, catalogue.BY_ID[object_type].mapping, provider_id="netbox:primary"
    )
    for name, value in expected.items():
        assert descriptor.attributes[name] == value, name
    # A polymorphic target cannot be an edge: one relationship names one target type,
    # and these point at whichever type the row happens to carry.
    assert all(
        relationship.rel not in {"scope", "termination", "parent"}
        for relationship in descriptor.relationships
    )


#: Every field NetBox replaced with a generic one, as (family, legacy, generic).
#: A given server serves exactly one of each pair, which is why both are mapped --
#: and why naming the wrong one in a column or a search list is a silent defect
#: rather than an error. The table is checked against the catalogue below, so it
#: cannot drift away from what is actually mapped.
_VERSION_REPLACED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("ip_prefix", "site", "scope"),
    ("cluster", "site", "scope"),
    ("circuit_termination", "site", "termination"),
    ("circuit_termination", "provider_network", "termination"),
    ("service", "device", "parent"),
    ("service", "virtual_machine", "parent"),
    ("contact", "group", "groups"),
    ("interface", "mac_address", "primary_mac_address"),
    ("vm_interface", "mac_address", "primary_mac_address"),
)


@pytest.mark.parametrize(("family", "legacy", "generic"), _VERSION_REPLACED_FIELDS)
def test_both_spellings_of_a_replaced_field_are_mapped(
    family: str, legacy: str, generic: str
) -> None:
    """Keeps the table above honest: a pair nothing maps would make the two tests vacuous."""
    attributes = catalogue.BY_ID[family].attributes
    assert legacy in attributes, f"{family}: {legacy} is not mapped"
    assert generic in attributes, f"{family}: {generic} is not mapped"


@pytest.mark.parametrize(("family", "legacy", "generic"), _VERSION_REPLACED_FIELDS)
def test_a_browse_column_never_names_the_spelling_a_current_netbox_dropped(
    family: str, legacy: str, generic: str
) -> None:
    """Columns take the generic spelling, so no operator gets a permanently empty one.

    This is the README's stated policy. ``ip_prefix`` broke it and shipped a blank
    Site column on every NetBox 4.2 and later, where a prefix carries ``scope``.
    """
    entry = catalogue.BY_ID[family]
    assert legacy not in entry.columns, (
        f"{family}: column {legacy!r} is absent on a supported NetBox; "
        f"use {generic!r}, which is what such a server serves"
    )


#: The families that surface a replaced field in their browse table, and the spelling
#: that has to appear there. Declared as intent rather than derived from the catalogue:
#: a derived set moves with the column it describes, so it could never catch the column
#: being deleted -- which is the regression this guards. Families whose replaced pair is
#: not a column at all are deliberately absent (a contact's `groups` and the two MAC
#: fields are searchable, never columns), so they are not forced to become one.
_REPLACED_FIELD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ip_prefix", "scope"),
    ("cluster", "scope"),
    ("circuit_termination", "termination"),
    ("service", "parent"),
)


@pytest.mark.parametrize(("family", "generic"), _REPLACED_FIELD_COLUMNS)
def test_a_family_that_browses_a_replaced_field_shows_the_generic_spelling(
    family: str, generic: str
) -> None:
    """The other half of the column rule: the right spelling is present, not just absent.

    Rejecting the legacy spelling alone would still pass if the column were dropped
    altogether, leaving the browse table with no scope at all rather than a wrong one.
    """
    entry = catalogue.BY_ID[family]
    assert generic in entry.columns, (
        f"{family}: browse columns no longer show {generic!r}, so the value a supported "
        f"NetBox serves is not in the table at all"
    )


@pytest.mark.parametrize(("family", "legacy", "generic"), _VERSION_REPLACED_FIELDS)
def test_a_searchable_field_stays_searchable_across_the_version_split(
    family: str, legacy: str, generic: str
) -> None:
    """Search lists both spellings, unlike columns.

    A search field costs no screen space, so listing both makes the value findable on
    either side of the rename; one alone leaves it unfindable on the other. Interface
    and VM interface listed only ``mac_address``, so searching by MAC matched nothing
    on a NetBox that serves ``primary_mac_address``.
    """
    entry = catalogue.BY_ID[family]
    if legacy not in entry.search_fields and generic not in entry.search_fields:
        pytest.skip(f"{family} does not offer {legacy}/{generic} for search")
    assert legacy in entry.search_fields and generic in entry.search_fields, (
        f"{family}: search offers only one of {legacy!r}/{generic!r}, so the value is "
        f"unfindable on the NetBox releases that serve the other"
    )


def test_the_legacy_spelling_still_resolves_on_an_older_netbox() -> None:
    """The pre-4.2 payload keeps working, edge included; that is why both are mapped."""
    descriptor = map_record(
        {
            "id": 3,
            "display": "ams-cluster",
            "name": "ams-cluster",
            "site": {"id": 5, "name": "AMS 01", "slug": "ams01"},
        },
        catalogue.BY_ID["cluster"].mapping,
        provider_id="netbox:primary",
    )
    assert descriptor.attributes["site"] == "ams01"
    assert descriptor.attributes["scope"] is None
    assert [
        (relationship.rel, relationship.target_external_id)
        for relationship in descriptor.relationships
    ] == [("site", "ams01")]


def test_a_pre_4_2_circuit_termination_traverses_to_both_of_its_legacy_targets() -> None:
    """Site and provider network are each a fixed foreign key, so each gets an edge.

    A termination lands either in a site or in a carrier's own network, and before 4.2
    those were two separate fields. Emitting an edge for only one of them makes the
    other reachable as text and nothing else.
    """
    in_a_site = map_record(
        {
            "id": 8,
            "display": "Termination A",
            "circuit": {"id": 4, "cid": "CID-001"},
            "term_side": "A",
            "site": {"id": 5, "name": "AMS 01", "slug": "ams01"},
        },
        catalogue.BY_ID["circuit_termination"].mapping,
        provider_id="netbox:primary",
    )
    assert {
        (relationship.rel, relationship.target_external_id)
        for relationship in in_a_site.relationships
    } == {("circuit", "4"), ("site", "ams01")}

    in_a_provider_network = map_record(
        {
            "id": 9,
            "display": "Termination Z",
            "circuit": {"id": 4, "cid": "CID-001"},
            "term_side": "Z",
            "provider_network": {"id": 12, "name": "Carrier Cloud"},
        },
        catalogue.BY_ID["circuit_termination"].mapping,
        provider_id="netbox:primary",
    )
    assert in_a_provider_network.attributes["provider_network"] == "Carrier Cloud"
    assert {
        (relationship.rel, relationship.target_external_id)
        for relationship in in_a_provider_network.relationships
    } == {("circuit", "4"), ("provider_network", "12")}

    # On 4.2+ neither field is served, so neither edge is emitted.
    generic = map_record(
        {
            "id": 10,
            "display": "Termination A",
            "circuit": {"id": 4, "cid": "CID-001"},
            "term_side": "A",
            "termination_type": "dcim.site",
            "termination": {"id": 5, "name": "AMS 01"},
        },
        catalogue.BY_ID["circuit_termination"].mapping,
        provider_id="netbox:primary",
    )
    assert generic.attributes["termination"] == "AMS 01"
    assert {relationship.rel for relationship in generic.relationships} == {"circuit"}


def test_the_config_rejects_families_nothing_serves_and_forgets_the_retired_setting() -> None:
    base = {"url": "https://netbox.example", "token_ref": "vault://netbox/token"}

    with pytest.raises(ValidationError) as query_error:
        NetBoxProviderConfig.model_validate({**base, "object_queries": {"nope": "x=1"}})
    message = str(query_error.value)
    assert "nope" in message  # the typo is named...
    assert "interface" in message  # ...and the valid ids are listed

    # A config written while this plugin still had its own object-type setting must
    # keep validating: extra="forbid" would otherwise reject it outright.
    for retired in (["interfaces", "rack"], ["*"], [], None):
        config = NetBoxProviderConfig.model_validate({**base, "object_types": retired})
        assert "object_types" not in config.model_dump(mode="json")

    # Including the comma-separated spelling it used to accept.
    assert "object_types" not in NetBoxProviderConfig.model_validate(
        {**base, "object_types": "rack, cable"}
    ).model_dump(mode="json")

    # An unknown key that was never ours is still an error.
    with pytest.raises(ValidationError):
        NetBoxProviderConfig.model_validate({**base, "nonsense": 1})
