# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deriving the catalogue from a NetBox schema, and checking it against the written one.

``tests/fixtures/netbox-4.3-schema.json`` is a real NetBox 4.3 OpenAPI document with the
prose and validation detail removed -- regenerate it with
``scripts/trim_netbox_schema.py``. Nothing in it was written to make these tests pass,
which is what makes the conformance test below worth anything.

The conformance test is the point of the whole module: every attribute the hand-written
catalogue declares must come back out of the schema with the same dotted path and the
same type. A difference is never cosmetic -- it is either the deriver misreading NetBox
or the catalogue having done so, and four such differences turned out to be the latter.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from copy import deepcopy
from typing import Any

import pytest

from hegemony_inventory_netbox import object_types as catalogue
from hegemony_inventory_netbox.derive import (
    DerivedCatalogue,
    custom_field_attributes,
    derive_catalogue,
    read_status,
    registered_catalogue,
)
from hegemony_inventory_netbox.overlay import DEFAULT_OVERLAY, REGISTERED_IDS, Overlay
from hegemony_inventory_sdk import DiscoveryResult, ObjectFieldMapping, ObjectTypeSpec

_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "netbox-4.3-schema.json"
_SCHEMA: dict[str, Any] = json.loads(_FIXTURE.read_text())
_DERIVED: DerivedCatalogue = derive_catalogue(_SCHEMA)
_BY_ENDPOINT = {entry.endpoint: entry for entry in _DERIVED.object_types}


def test_the_fixture_is_a_real_netbox_release() -> None:
    assert _DERIVED.source_version is not None
    assert _DERIVED.source_version.startswith("4.3")


def test_query_parameters_become_a_separate_query_schema() -> None:
    """A response field is not proof that NetBox accepts it as a filter.

    Discovery therefore carries the GET operation's query parameters separately,
    including reusable OpenAPI parameter components and their enum/type metadata.
    """
    schema = deepcopy(_SCHEMA)
    schema.setdefault("components", {}).setdefault("parameters", {})["Search"] = {
        "name": "q",
        "in": "query",
        "description": "Free text search.",
        "schema": {"type": "string"},
    }
    schema["paths"]["/api/dcim/devices/"]["get"]["parameters"] = [
        {"$ref": "#/components/parameters/Search"},
        {
            "name": "status",
            "in": "query",
            "schema": {"type": "string", "enum": ["active", "offline"]},
        },
        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
        {"name": "ignored", "in": "header", "schema": {"type": "string"}},
    ]

    devices = next(
        entry
        for entry in derive_catalogue(schema).object_types
        if entry.endpoint == "/api/dcim/devices/"
    )

    assert devices.query_schema["properties"] == {
        "q": {"type": "string", "description": "Free text search."},
        "status": {"type": "string", "enum": ["active", "offline"]},
        # The fixture deliberately strips django-filter parameters; the tenant-aware
        # fallback remains present for the cross-organization mapping workflow.
        "tenant": {"type": "string", "description": "NetBox tenant slug."},
    }


# ── The conformance gate ──────────────────────────────────────────────────────
#
# The shipped catalogue is now the deriver's own output (``scripts/generate_catalogue``),
# so the old gate -- "every hand-written family comes back from the schema" -- has
# nothing left to compare and would pass whatever either side did. What is still worth
# holding is the other direction: that the checked-in file is what the current deriver
# and overlay produce, and that it never loses a family whose id is a persistence
# contract.


def test_the_shipped_catalogue_is_what_the_deriver_produces() -> None:
    """Regenerating changes nothing, or the checked-in catalogue is stale.

    A generated file that nobody regenerated is worse than a hand-written one: it reads
    as the deriver's output while stating something the deriver no longer says. This is
    the whole gate, and it fails on any change to the deriver, the overlay or the
    recorded schema that was not regenerated -- the README's family table included.
    """
    spec = importlib.util.spec_from_file_location(
        "_generate_catalogue",
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "generate_catalogue.py",
    )
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    assert generator.main(["--check"]) == 0, (
        "the shipped catalogue is not what the deriver produces -- run "
        "'uv run python scripts/generate_catalogue.py' and commit the result"
    )


def test_the_shipped_catalogue_keeps_every_id_it_has_promised() -> None:
    """No regeneration may drop a family whose id this plugin already registered.

    An object-type id is stored in ``inventory_provider_configs.object_types``, in the
    ``object_type`` of every synced row, and inside user-authored flow definitions.
    Losing one does not remove a feature, it orphans data -- so the fifty ids frozen in
    :data:`~hegemony_inventory_netbox.overlay.REGISTERED_IDS` are a floor the generated
    catalogue may rise above and never fall below.
    """
    shipped = {entry.id for entry in catalogue.OBJECT_TYPES}
    promised = set(REGISTERED_IDS.values())
    assert promised <= shipped, f"the catalogue no longer offers: {sorted(promised - shipped)}"


def test_the_shipped_catalogue_stores_what_netbox_serves() -> None:
    """Every shipped family carries every attribute the schema declares for it.

    This is the trade the deriver makes and the reason the catalogue is generated at
    all: it stores what NetBox serves rather than what someone selected, so a family is
    only ever narrower than the schema where the overlay says so on purpose.
    """
    thin = [
        entry.id
        for entry in catalogue.OBJECT_TYPES
        if entry.endpoint in _BY_ENDPOINT
        and len(entry.attributes) < len(_BY_ENDPOINT[entry.endpoint].attributes)
    ]
    assert not thin, f"shipped families carry fewer attributes than the schema: {thin}"


# ── App-qualified ids ─────────────────────────────────────────────────────────


def test_app_qualified_ids_need_no_table_of_exceptions() -> None:
    """Every family the schema serves gets a distinct id from its endpoint alone.

    This is why the deriver can name families nobody wrote down -- a NetBox plugin's
    models included. A bare singular cannot: NetBox serves interfaces under both
    ``dcim`` and ``virtualization``.
    """
    ids = [entry.id for entry in _DERIVED.object_types]
    assert len(set(ids)) == len(ids)
    assert "dcim.interfaces" in ids
    assert "virtualization.interfaces" in ids


def test_every_registered_id_publishes_its_qualified_spelling() -> None:
    """The alias table covers the whole catalogue, both ways.

    It is what an adoption of the derived ids would migrate with, so a family missing
    from it is a family that would be left behind.
    """
    assert set(catalogue.LEGACY_OBJECT_TYPE_IDS) == set(catalogue.ALL_OBJECT_TYPE_IDS)
    assert len(catalogue.OBJECT_TYPE_IDS_BY_QUALIFIED) == len(catalogue.LEGACY_OBJECT_TYPE_IDS)


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_a_qualified_id_names_the_family_the_deriver_produced(
    entry: catalogue.NetBoxObjectType,
) -> None:
    assert catalogue.LEGACY_OBJECT_TYPE_IDS[entry.id] == _BY_ENDPOINT[entry.endpoint].id


def test_a_plugin_endpoint_qualifies_by_its_own_app() -> None:
    """Two NetBox plugins cannot collide, and neither can a plugin with a core app."""
    assert catalogue.qualified_object_type_id("/api/plugins/netbox_dns/records/") == (
        "netbox_dns.records"
    )
    assert catalogue.qualified_object_type_id("/api/dcim/interfaces/") == "dcim.interfaces"


# ── Rules the deriver applies ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("object_type", "name_field"),
    [
        ("ipam.prefixes", "prefix"),
        ("ipam.ip_addresses", "address"),
        ("circuits.circuits", "cid"),
        ("dcim.devices", "name"),
    ],
)
def test_a_family_is_named_by_the_property_someone_sets(object_type: str, name_field: str) -> None:
    """NetBox has no universal name property, and its computed ones are ``readOnly``.

    ``Prefix`` requires ``children`` and ``_depth`` as well as ``prefix``; picking the
    first required property without that rule names a prefix by how many child prefixes
    it has.
    """
    derived = {entry.id: entry for entry in _DERIVED.object_types}[object_type]
    assert derived.name_field == name_field
    assert derived.columns[0] == name_field


def test_no_derived_column_names_a_spelling_this_netbox_replaced() -> None:
    """A column costs screen space whether or not anything ever lands in it.

    The written catalogue had to be corrected for this by hand once, on ``ip_prefix``.
    The rule makes it unrepeatable.
    """
    for endpoint, replaced in DEFAULT_OVERLAY.replaced_fields.items():
        derived = _BY_ENDPOINT[endpoint]
        for field in replaced:
            assert field.legacy in derived.attributes, (
                f"{endpoint}: {field.legacy!r} is declared replaced but not mapped"
            )
            assert field.legacy not in derived.columns, (
                f"{endpoint}: {field.legacy!r} is a column a current NetBox never fills"
            )


def test_both_spellings_of_a_replaced_field_stay_searchable() -> None:
    """The mirror of the column rule, and the reason the two share one table.

    A search field costs no screen space and an absent attribute simply never matches,
    so naming both keeps a value findable on either side of a rename. Naming one leaves
    it unfindable on the other, which is how an interface stopped being searchable by
    its MAC address.
    """
    for endpoint, replaced in DEFAULT_OVERLAY.replaced_fields.items():
        derived = _BY_ENDPOINT[endpoint]
        for field in replaced:
            for spelling in (field.legacy, field.generic):
                if spelling not in derived.attributes:
                    continue
                assert spelling in derived.search_fields, (
                    f"{endpoint}: {spelling!r} is mapped but not searchable, so the "
                    f"pair {field.legacy!r}/{field.generic!r} is findable on only one "
                    "side of the rename"
                )


@pytest.mark.parametrize(
    ("object_type", "attribute"),
    [("dcim.cables", "type"), ("ipam.fhrp_groups", "protocol"), ("ipam.fhrp_groups", "auth_type")],
)
def test_a_flat_enum_is_not_read_through_a_value_key(object_type: str, attribute: str) -> None:
    """NetBox renders some choice fields flat and most as ``{value, label}``.

    Nothing in the API reference distinguishes them, and reading ``<name>.value`` from a
    flat one yields ``None`` on every record forever. These three shipped that way.
    """
    derived = {entry.id: entry for entry in _DERIVED.object_types}[object_type]
    assert derived.attributes[attribute].path == attribute
    written = catalogue.BY_ID[catalogue.OBJECT_TYPE_IDS_BY_QUALIFIED[object_type]]
    assert written.attributes[attribute].path == attribute


def test_a_choice_field_carries_its_choices_into_the_field_schema() -> None:
    """What a written catalogue cannot keep correct by hand, and so does not keep.

    A schema states the exact choice set for every status, type and role field, which is
    the difference between offering an operator a filter and offering them a text box.
    """
    status = {entry.id: entry for entry in _DERIVED.object_types}["dcim.sites"].attributes["status"]
    assert status.enum == ("planned", "staging", "active", "decommissioning", "retired")
    assert status.schema["enum"] == list(status.enum)
    # And the shipped catalogue carries them, which a hand-written one never did:
    # keeping thirty choice lists correct by hand is not work anyone would choose.
    with_choices = [
        f"{entry.id}.{name}"
        for entry in catalogue.OBJECT_TYPES
        for name, attribute in entry.attributes.items()
        if attribute.enum
    ]
    assert len(with_choices) > 100, (
        f"the shipped catalogue declares only {len(with_choices)} choice sets; "
        "deriving them is the difference between a filter and a text box"
    )


def test_a_nullable_choice_still_admits_null() -> None:
    """JSON Schema intersects ``type`` and ``enum``, so the null has to be listed."""
    attribute = catalogue.Attr("status.value", "string", True, ("active", "offline"))
    assert attribute.schema == {"type": ["string", "null"], "enum": ["active", "offline", None]}


def test_an_edge_cites_the_external_id_even_where_the_attribute_does_not() -> None:
    """An attribute and an edge want different things from the same reference.

    The attribute stores what a person reads -- a VLAN's number, a site's slug. The edge
    has to cite the target's external id, because that is the key the platform stored it
    under and the only value that resolves back to it. Reading the natural key into both
    produces edges that point at nothing.
    """
    prefixes = {entry.id: entry for entry in _DERIVED.object_types}["ipam.prefixes"]
    edges = {edge.rel: (edge.target_object_type, edge.source) for edge in prefixes.relationships}
    assert prefixes.attributes["vlan"].path == "vlan.vid"
    assert edges["vlan"] == ("ipam.vlans", "vlan.id")
    # Sites are the exception: the platform materialises them through the site contract,
    # which keys them on their slug.
    assert edges["site"] == ("dcim.sites", "site.slug")
    assert all(
        edge.target_object_type in {entry.id for entry in _DERIVED.object_types}
        for edge in prefixes.relationships
    )


def test_a_serializer_variant_still_resolves_to_the_family_it_renders() -> None:
    """``/api/dcim/devices/`` returns ``DeviceWithConfigContext`` rows; every reference
    to a device resolves to ``BriefDevice``. If the two do not meet, a device has
    attributes and nothing pointing at it -- no interface, no port, no module.
    """
    interfaces = {entry.id: entry for entry in _DERIVED.object_types}["dcim.interfaces"]
    edges = {edge.rel: (edge.target_object_type, edge.source) for edge in interfaces.relationships}
    assert edges["device"] == ("dcim.devices", "device.id")
    vm_interfaces = {entry.id: entry for entry in _DERIVED.object_types}[
        "virtualization.interfaces"
    ]
    vm_edges = {edge.rel: edge.target_object_type for edge in vm_interfaces.relationships}
    assert vm_edges["virtual_machine"] == "virtualization.virtual_machines"


_CORE_CONTRACT_IDS = frozenset({"device", "site"})


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_every_shipped_edge_points_at_a_family_something_serves(
    entry: catalogue.NetBoxObjectType,
) -> None:
    """An edge names a target the platform can actually reach.

    The deriver names families after their endpoints and the catalogue re-keys the fifty
    it has promised onto the ids it registers. Re-key an entry and forget its edges and
    every edge into that family points at an id nothing serves -- a graph that looks
    whole and resolves nowhere. Devices and sites are the deliberate exception: they are
    reached through the platform's own contract rather than as generic objects.
    """
    shipped = {other.id for other in catalogue.OBJECT_TYPES} | set(_CORE_CONTRACT_IDS)
    for relationship in entry.relationships:
        assert relationship.target_object_type in shipped, (
            f"{entry.id}.{relationship.rel} points at "
            f"{relationship.target_object_type!r}, which no family serves"
        )


def test_denormalised_counters_are_dropped_unless_a_family_asks_for_one() -> None:
    """NetBox hangs child counts off parents; they describe the children, not the object."""
    sites = {entry.id: entry for entry in _DERIVED.object_types}["dcim.sites"]
    assert "device_count" not in sites.attributes
    chassis = {entry.id: entry for entry in _DERIVED.object_types}["dcim.virtual_chassis"]
    assert "member_count" in chassis.attributes


# ── The overlay ───────────────────────────────────────────────────────────────


def test_the_overlay_names_nothing_this_netbox_does_not_serve() -> None:
    """A stale overlay entry is a silent correction to a field that no longer exists."""
    stale = [warning for warning in _DERIVED.warnings if warning.startswith("overlay ")]
    assert stale == []


def test_the_overlay_declares_no_legacy_spelling_this_schema_already_serves() -> None:
    assert [w for w in _DERIVED.warnings if "no longer needed" in w] == []


def test_a_stock_netbox_derives_without_a_single_warning() -> None:
    """Every shape a stock 4.3 serves is one the deriver knows what to do with.

    This is what makes a warning worth reading. If the overlay only covered the families
    the written catalogue happens to offer, the deriver would warn on a stock install
    and nobody would look; because it covers all of them, a warning means this NetBox
    serves something new -- a later release, or a plugin's own model.
    """
    assert list(_DERIVED.warnings) == []


def test_an_undeclared_generic_reference_warns_rather_than_guessing() -> None:
    bare = derive_catalogue(_SCHEMA, overlay=Overlay(untyped_paths={}, untyped_objects=frozenset()))
    assert any("generic reference" in warning for warning in bare.warnings)
    prefixes = {entry.id: entry for entry in bare.object_types}["ipam.prefixes"]
    assert prefixes.attributes["scope"].path == "scope"
    assert prefixes.attributes["scope"].json_type == "object"


def test_a_skipped_endpoint_is_never_offered() -> None:
    """NetBox's own plumbing is not inventory, whatever else it is."""
    endpoints = {entry.endpoint for entry in _DERIVED.object_types}
    assert "/api/extras/webhooks/" not in endpoints
    assert "/api/extras/custom-fields/" not in endpoints
    assert endpoints & DEFAULT_OVERLAY.skip_endpoints == set()


def test_a_credential_store_plugin_is_never_offered() -> None:
    """netbox-secrets serves every secret's hash; an inventory copy is an attack surface.

    Each withheld path is given a real list endpoint's operations, so it would be
    offered like any other plugin family if the skip did not hold, and a plugin that is
    not a credential store is offered alongside to show the skip is that narrow.
    """
    schema = deepcopy(_SCHEMA)
    operations = schema["paths"]["/api/tenancy/tenants/"]
    withheld = {
        "/api/plugins/secrets/secrets/",
        "/api/plugins/secrets/user-keys/",
        "/api/plugins/netbox_secretstore/secrets/",
    }
    for path in (*withheld, "/api/plugins/netbox_dns/records/"):
        schema["paths"][path] = deepcopy(operations)

    endpoints = {entry.endpoint for entry in derive_catalogue(schema).object_types}

    assert endpoints & withheld == set()
    assert "/api/plugins/netbox_dns/records/" in endpoints


# ── Custom fields ─────────────────────────────────────────────────────────────


_CUSTOM_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "name": "hegemony_ssh_ref",
        "type": {"value": "text", "label": "Text"},
        "object_types": ["dcim.device"],
    },
    {
        "name": "rack_units_free",
        "type": {"value": "integer", "label": "Integer"},
        "object_types": ["dcim.rack", "dcim.device"],
    },
    {"name": "audited", "type": {"value": "boolean"}, "object_types": ["ipam.prefix"]},
)


def test_custom_fields_reach_the_families_they_apply_to() -> None:
    """The whole reason to ask an instance rather than ship a catalogue.

    Every NetBox has custom fields and no two have the same ones, so nothing written in
    advance can carry them -- the written catalogue maps not one.
    """
    derived = derive_catalogue(_SCHEMA, custom_fields=_CUSTOM_FIELDS)
    by_id = {entry.id: entry for entry in derived.object_types}
    device = by_id["dcim.devices"].attributes
    assert device["custom_field_hegemony_ssh_ref"].path == "custom_fields.hegemony_ssh_ref"
    assert device["custom_field_rack_units_free"].json_type == "integer"
    assert "custom_field_audited" not in device
    assert "custom_field_audited" in by_id["ipam.prefixes"].attributes


def test_a_custom_field_is_matched_by_netboxs_own_content_type_label() -> None:
    """Which an OpenAPI document never states, so it is rebuilt from the endpoint.

    The component name will not do: ``/api/dcim/devices/`` renders through a serializer
    called ``DeviceWithConfigContext``.
    """
    assert _DERIVED.content_types["dcim.devices"] == "dcim.device"
    assert _DERIVED.content_types["ipam.ip_addresses"] == "ipam.ipaddress"
    assert _DERIVED.content_types["dcim.virtual_chassis"] == "dcim.virtualchassis"


def _served_component(schema: dict[str, Any], endpoint: str) -> str:
    """The schema component a list endpoint's ``results`` items are, e.g. ``VMInterface``."""
    body = schema["paths"][endpoint]["get"]["responses"]["200"]["content"]["application/json"]
    page = body["schema"]["$ref"].rsplit("/", 1)[-1]
    items = schema["components"]["schemas"][page]["properties"]["results"]["items"]
    return items["$ref"].rsplit("/", 1)[-1]


@pytest.mark.parametrize("fixture", ["netbox-4.3-schema.json", "netbox-4.7-schema.json"])
def test_a_model_not_named_after_its_endpoint_still_gets_netboxs_own_label(
    fixture: str,
) -> None:
    """``/api/virtualization/interfaces/`` serves ``VMInterface``, not an "interface".

    Singularising the segment gave ``virtualization.interface``, a label NetBox attaches
    no custom field to, so a VM interface's custom fields were never derived. The
    component an endpoint returns is the model's own name (less the serializer
    decoration), so every family is checked against it here: the two the overlay
    corrects are the only two that differ, on both recorded releases.
    """
    schema = json.loads(_FIXTURE.with_name(fixture).read_text())
    custom_fields = (
        {
            "name": "vlan_pool",
            "type": {"value": "text", "label": "Text"},
            "object_types": ["virtualization.vminterface"],
        },
    )
    derived = derive_catalogue(schema, custom_fields=custom_fields)
    by_id = {entry.id: entry for entry in derived.object_types}

    assert derived.content_types["virtualization.interfaces"] == "virtualization.vminterface"
    assert derived.content_types["extras.tagged_objects"] == "extras.taggeditem"
    vm_interface = by_id["virtualization.interfaces"].attributes
    assert vm_interface["custom_field_vlan_pool"].path == "custom_fields.vlan_pool"
    assert "custom_field_vlan_pool" not in by_id["dcim.interfaces"].attributes

    for entry in derived.object_types:
        component = _served_component(schema, entry.endpoint).removesuffix("WithConfigContext")
        expected = f"{entry.app.replace('-', '_')}.{component.lower()}"
        assert derived.content_types[entry.id] == expected, entry.endpoint


def test_a_custom_field_applying_nowhere_relevant_is_ignored() -> None:
    assert custom_field_attributes(_CUSTOM_FIELDS, content_type="circuits.circuit") == {}


# ── The status probe ──────────────────────────────────────────────────────────


def test_the_status_probe_reads_the_release_and_the_installed_plugins() -> None:
    status = read_status(
        {
            "netbox-version": "4.3.0",
            "netbox-full-version": "4.3.0-Docker-3.3.0",
            "plugins": {"netbox_dns": "1.1.4"},
            "django-version": "5.1.8",
        }
    )
    assert status.full_version == "4.3.0-Docker-3.3.0"
    assert status.plugins == {"netbox_dns": "1.1.4"}
    assert status.safe_details == {
        "netbox_version": "4.3.0",
        "netbox_full_version": "4.3.0-Docker-3.3.0",
        "netbox_plugins": {"netbox_dns": "1.1.4"},
    }


def test_the_status_probe_reports_nothing_it_was_not_told() -> None:
    """An older or proxied NetBox may answer with less; that is not an error."""
    assert read_status({}).safe_details == {}
    assert read_status({"plugins": []}).plugins == {}


# ── The discovery contract ────────────────────────────────────────────────────


def test_a_discovery_result_refuses_halves_that_disagree() -> None:
    """A spec without its mapping is an object type nothing can fetch, and the reverse
    is a mapping for something the platform will never render. Neither is storable, so
    the result rejects them where they are built rather than where they are used.
    """
    spec = ObjectTypeSpec(id="dcim.racks", display_name="Rack")
    mapping = ObjectFieldMapping(object_type="dcim.racks", identity="id", name_field="name")

    assert DiscoveryResult(object_types=(spec,), mappings={"dcim.racks": mapping})

    with pytest.raises(ValueError, match="dcim.racks"):
        DiscoveryResult(object_types=(spec,))
    with pytest.raises(ValueError, match="dcim.racks"):
        DiscoveryResult(mappings={"dcim.racks": mapping})


def test_an_empty_discovery_result_is_valid() -> None:
    """An instance that serves nothing this provider understands is an answer, not an error."""
    assert DiscoveryResult().object_types == ()


def test_re_keying_leaves_a_family_the_catalogue_does_not_know_alone() -> None:
    """The derived id is what a new family arrives under, because nothing else names it."""
    catalogued = registered_catalogue(_DERIVED)
    by_id = {entry.id: entry for entry in catalogued}
    assert "circuits.circuit_groups" in by_id
    assert by_id["circuits.circuit_groups"].endpoint == "/api/circuits/circuit-groups/"


def test_the_catalogue_binds_devices_and_sites_onto_the_platforms_own_tables() -> None:
    """Devices and sites are families now, and the binding is what keeps that safe.

    Offering them as generic object types alone would store every device twice: once as
    an object and once, through the old contract, as a device. The binding says the
    stored rows *are* the devices and sites tables, so there is one read and one row.
    """
    bound = {entry.id: entry.binding for entry in catalogue.OBJECT_TYPES if entry.binding}
    assert set(bound) == {"device", "site", "region"}
    assert bound["device"].target == "device"
    assert bound["site"].target == "site"
    # A region becomes a site row too -- that is how a hierarchy living in another family
    # becomes the platform's single-parent site tree -- and the prefix is what stops a
    # region and a site of the same slug colliding on (provider_id, external_id).
    assert bound["region"].target == "site"
    assert bound["region"].external_id_prefix == "region:"
    assert bound["site"].external_id_prefix == ""


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_every_edge_cites_the_key_its_target_is_stored_under(
    entry: catalogue.NetBoxObjectType,
) -> None:
    """One key per family, for storing a row and for citing it.

    This is the invariant the whole line of work exists to protect, stated once over the
    whole catalogue. A site used to be stored under its numeric id while every edge into
    it cited its slug: every row materialised, every link dangled, and nothing failed.
    Checking each edge's key against the identity of the family it reaches is what makes
    that loud -- and it has to be checked against the *target family*, not against the
    same table both were read from, or it is a tautology that moves with the mistake.
    """
    by_id = {other.id: other for other in catalogue.OBJECT_TYPES}
    for edge in entry.relationships:
        target = by_id.get(edge.target_object_type)
        if target is None:
            continue
        cited = edge.source.rsplit(".", 1)[-1]
        assert cited == target.identity, (
            f"{entry.id}.{edge.rel} cites {edge.target_object_type}.{cited}, but a "
            f"{target.id} row is stored under its {target.identity!r} -- the edge "
            "resolves to nothing"
        )


def test_the_site_tree_is_keyed_the_way_the_platform_already_stored_it() -> None:
    """Sites keep their slug, which is the key the device/site contract used.

    Not an arbitrary choice: an upgrade re-uses the site rows an earlier sync stored
    rather than making a second set beside them, and a site selector already holding a
    slug goes on resolving.
    """
    by_id = {entry.id: entry for entry in catalogue.OBJECT_TYPES}
    assert by_id["site"].identity == "slug"
    assert by_id["region"].identity == "id"
    site_edge = next(edge for edge in by_id["device"].relationships if edge.rel == "site")
    assert site_edge.source == "site.slug"


def test_real_netbox_47_schema_preserves_strings_and_has_no_opaque_warnings() -> None:
    schema = json.loads(_FIXTURE.with_name("netbox-4.7-schema.json").read_text())
    result = derive_catalogue(schema)
    assert result.source_version is not None
    assert result.source_version.startswith("4.7")
    assert len(result.object_types) == 117
    assert result.warnings == ()
    by_id = {entry.id: entry for entry in registered_catalogue(result)}
    for family, fields in {
        "ip_address": ["dns_name"],
        "contact": ["email", "link"],
        "cable": ["color"],
        "circuit_type": ["color"],
    }.items():
        for name in fields:
            assert by_id[family].spec.field_schema["properties"][name]["type"] == "string"
            assert by_id[family].mapping.field_map[name] == name
    assert by_id["extras.config_context_profiles"].mapping.field_map["schema"] == "schema"
    assert by_id["tenancy.contact_assignments"].mapping.field_map["object"] == "object.display"
