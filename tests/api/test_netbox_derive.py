# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
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

import json
import pathlib
from typing import Any

import pytest

from hegemony_inventory_netbox import object_types as catalogue
from hegemony_inventory_netbox.derive import (
    DerivedCatalogue,
    custom_field_attributes,
    derive_catalogue,
    read_status,
)
from hegemony_inventory_netbox.overlay import DEFAULT_OVERLAY, Overlay

_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "netbox-4.3-schema.json"
_SCHEMA: dict[str, Any] = json.loads(_FIXTURE.read_text())
_DERIVED: DerivedCatalogue = derive_catalogue(_SCHEMA)
_BY_ENDPOINT = {entry.endpoint: entry for entry in _DERIVED.object_types}


def test_the_fixture_is_a_real_netbox_release() -> None:
    assert _DERIVED.source_version is not None
    assert _DERIVED.source_version.startswith("4.3")


# ── The conformance gate ──────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_the_deriver_reproduces_every_written_family(entry: catalogue.NetBoxObjectType) -> None:
    """Each hand-written family is a collection endpoint the schema actually serves."""
    assert entry.endpoint in _BY_ENDPOINT, f"{entry.id} names an endpoint NetBox 4.3 does not serve"


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_the_deriver_reproduces_every_written_attribute(
    entry: catalogue.NetBoxObjectType,
) -> None:
    """Every written attribute comes back with the same path and type.

    Where it does not, exactly one of the two is wrong about NetBox. Deciding which is
    the work; this test is what makes the question come up at all.
    """
    derived = _BY_ENDPOINT[entry.endpoint]
    for name, attribute in entry.attributes.items():
        found = derived.attributes.get(name)
        assert found is not None, f"{entry.id}.{name} is not derivable from the schema"
        assert (found.path, found.json_type) == (attribute.path, attribute.json_type), (
            f"{entry.id}.{name}: the catalogue reads {attribute.path!r} as "
            f"{attribute.json_type}, the schema says {found.path!r} as {found.json_type}"
        )


def test_the_deriver_finds_more_than_the_written_catalogue_declares() -> None:
    """The schema is a superset: the written catalogue is a selection from it.

    This is the trade the deriver makes. It stores what NetBox serves rather than what
    someone chose to keep, so a derived family carries more attributes than a written
    one -- and never fewer, which is the previous test.
    """
    written = sum(len(entry.attributes) for entry in catalogue.OBJECT_TYPES)
    derived = sum(len(_BY_ENDPOINT[entry.endpoint].attributes) for entry in catalogue.OBJECT_TYPES)
    assert derived > written


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
    assert not any(
        attribute.enum
        for entry in catalogue.OBJECT_TYPES
        for attribute in entry.attributes.values()
    ), "the written catalogue declares no choice sets, which is the point being made"


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


_CORE_CONTRACT_IDS = {"device": "dcim.devices", "site": "dcim.sites"}


@pytest.mark.parametrize("entry", catalogue.OBJECT_TYPES, ids=lambda entry: entry.id)
def test_the_deriver_reproduces_every_written_edge(entry: catalogue.NetBoxObjectType) -> None:
    """Every written relationship comes back pointing at the same family by the same key.

    Matched on target and source rather than on the edge's label: the written catalogue
    names some edges after the type they reach (``tenant_group`` for the ``group``
    property) while the deriver names them after the property, which is derivable and
    disambiguated by the target. What has to agree is where the edge goes and what it
    cites to get there.
    """
    derived = {
        (edge.target_object_type, edge.source)
        for edge in _BY_ENDPOINT[entry.endpoint].relationships
    }
    for relationship in entry.relationships:
        target = catalogue.LEGACY_OBJECT_TYPE_IDS.get(
            relationship.target_object_type
        ) or _CORE_CONTRACT_IDS.get(relationship.target_object_type)
        assert (target, relationship.source) in derived, (
            f"{entry.id}.{relationship.rel} points at {target} via "
            f"{relationship.source!r}; the deriver produced {sorted(derived)}"
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
