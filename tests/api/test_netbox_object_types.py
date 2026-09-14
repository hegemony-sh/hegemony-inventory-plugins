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
_OBJECT_TYPE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


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
    assert entry.id not in _CORE_RESOURCES, "devices and sites have their own contract"
    assert _ENDPOINT.match(entry.endpoint), entry.endpoint
    # Every NetBox object has a numeric primary key, and it is what related objects
    # cite, so it is the only identity that works for every family.
    assert entry.identity == "id"
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


def test_catalogue_ids_are_unique_and_an_unset_config_offers_them_all() -> None:
    ids = [entry.id for entry in catalogue.OBJECT_TYPES]
    assert len(ids) == len(set(ids)), "duplicate ids would collide in the platform registry"
    assert tuple(sorted(ids)) == catalogue.ALL_OBJECT_TYPE_IDS
    # The config is a ceiling, and an unset ceiling stops nothing: what a provider
    # actually reads is the platform's own object-type selection, which cannot widen
    # past what the provider declares it serves.
    assert catalogue.resolve_object_type_ids(None) == catalogue.ALL_OBJECT_TYPE_IDS
    assert catalogue.resolve_object_type_ids([catalogue.WILDCARD]) == catalogue.ALL_OBJECT_TYPE_IDS


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


def test_a_provider_reads_only_the_families_its_config_names() -> None:
    every = _CORE_RESOURCES | set(catalogue.ALL_OBJECT_TYPE_IDS)
    assert _object_types(_provider()) == every
    assert _object_types(_provider({"object_types": [catalogue.WILDCARD]})) == every
    scoped = _provider({"object_types": ["interface", "rack"]})
    assert _object_types(scoped) == _CORE_RESOURCES | {"interface", "rack"}
    # Devices and sites are never deselectable: they are the platform's own tables.
    assert _object_types(_provider({"object_types": []})) == _CORE_RESOURCES


@pytest.mark.asyncio
async def test_a_family_the_config_excludes_is_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not reading a family means refusing it, not silently returning nothing.

    The platform skips what a provider does not declare, so this only bites a direct
    caller -- and for that caller "not supported" is the honest answer.
    """
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, handler)
    provider = _provider({"object_types": ["rack"]})

    with pytest.raises(InventoryProviderError) as excinfo:
        await provider.list_objects("interface", context=_context("list_objects:interface"))
    assert "not supported" in excinfo.value.envelope.message
    with pytest.raises(InventoryProviderError):
        await provider.get_object("interface", "1", context=_context("get_object:interface"))
    assert requested == [], "an excluded family must not reach NetBox at all"


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
    assert interface.attributes == {
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
        "mark_connected": False,
        "description": "uplink",
    }
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
    provider = _provider({"object_types": [catalogue.WILDCARD]})

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


def test_the_config_rejects_families_nothing_serves_and_keeps_the_wildcard() -> None:
    base = {"url": "https://netbox.example", "token_ref": "vault://netbox/token"}

    with pytest.raises(ValidationError) as excinfo:
        NetBoxProviderConfig.model_validate({**base, "object_types": ["interfaces", "rack"]})
    message = str(excinfo.value)
    assert "interfaces" in message  # the typo is named...
    assert "interface" in message  # ...and the valid ids are listed

    with pytest.raises(ValidationError) as query_error:
        NetBoxProviderConfig.model_validate({**base, "object_queries": {"nope": "x=1"}})
    assert "nope" in str(query_error.value)

    # The wildcard is stored as written, so a later plugin version that adds a family
    # starts reading it without anyone editing the provider.
    wildcard = NetBoxProviderConfig.model_validate({**base, "object_types": ["*"]})
    assert wildcard.object_types == ["*"]
    assert wildcard.model_dump(mode="json")["object_types"] == ["*"]
    assert len(wildcard.enabled_object_types) == len(catalogue.ALL_OBJECT_TYPE_IDS)

    # A comma-separated string is accepted the way auth_fallback_schemes is.
    assert NetBoxProviderConfig.model_validate(
        {**base, "object_types": "rack, cable"}
    ).enabled_object_types == ("cable", "rack")
