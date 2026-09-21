# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The transform vocabulary: what it performs, and what it refuses."""

from __future__ import annotations

from typing import Any

import pytest

from hegemony_inventory_sdk import (
    TRANSFORMABLE_FIELDS,
    TRANSFORMS,
    ProjectionBinding,
    apply_transforms,
)


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("host", "10.0.0.1/24", "10.0.0.1"),
        # IPv6 survives: only the first slash is a separator, and the colons are not.
        ("host", "2001:db8::1/64", "2001:db8::1"),
        # A bare address or hostname has nothing to strip and must come back whole.
        ("host", "10.0.0.1", "10.0.0.1"),
        ("host", "core-01.example.net", "core-01.example.net"),
        ("lower", "IOS-XE", "ios-xe"),
        ("upper", "fdo21120u8n", "FDO21120U8N"),
        ("strip", "  serial  ", "serial"),
        ("first", ["a", "b"], "a"),
        ("first", [], None),
        # A scalar where a list was expected passes through rather than being indexed,
        # so a field that is sometimes one and sometimes the other is safe to declare.
        ("first", "a", "a"),
        ("first", {"slug": "a"}, {"slug": "a"}),
    ],
)
def test_each_transform_does_what_its_name_says(name: str, value: Any, expected: Any) -> None:
    assert TRANSFORMS[name](value) == expected


@pytest.mark.parametrize("name", sorted(TRANSFORMS))
@pytest.mark.parametrize("value", [None, 7, True, {"a": 1}, ["x"]])
def test_no_transform_raises_on_any_value(name: str, value: Any) -> None:
    """Totality is the property that lets transforms compose in any order.

    A transform that raised on a shape it had nothing to say about would make every
    chain order-dependent, and the order is not something a binding author should have
    to reason about.
    """
    TRANSFORMS[name](value)


def test_transforms_apply_in_the_order_they_are_written() -> None:
    """Order is the author's to choose, so it has to be honoured rather than sorted.

    Chosen because the two orders genuinely disagree: ``lower`` has nothing to say
    about a list and passes it through, so unwrapping first is what lets it act at all.
    """
    assert apply_transforms(["ABC"], ("first", "lower")) == "abc"
    assert apply_transforms(["ABC"], ("lower", "first")) == "ABC"


def test_a_chain_is_normalised_to_a_tuple_whatever_sequence_it_arrived_as() -> None:
    """A binding round-trips through JSON, where a tuple can only come back as a list.

    Without normalising, a restored binding would compare unequal to the stored one and
    every equality check downstream would quietly be about the serializer instead.
    """
    binding = ProjectionBinding(
        target="device",
        object_type="d",
        hostname="primary_ip4",
        transforms={"hostname": ["host"]},
    )
    assert binding.transforms == {"hostname": ("host",)}


def test_a_bare_string_is_refused_rather_than_read_one_character_at_a_time() -> None:
    """``str`` is itself a ``Sequence[str]``, so the obvious shorthand is a trap.

    Accepted quietly, ``"host"`` would be four transforms named h, o, s and t. Refusing
    it with the fix in the message is the only reading that cannot surprise anyone.
    """
    with pytest.raises(ValueError, match=r"must be a list, not the bare string 'host'"):
        ProjectionBinding(
            target="device",
            object_type="d",
            hostname="primary_ip4",
            transforms={"hostname": "host"},
        )


def test_a_binding_rejects_a_transform_this_platform_cannot_perform() -> None:
    """Unknown means fail, not skip: the raw value would look like a converted one."""
    with pytest.raises(ValueError, match="unknown transform 'reverse_dns'"):
        ProjectionBinding(
            target="device",
            object_type="d",
            hostname="primary_ip4",
            transforms={"hostname": ["host", "reverse_dns"]},
        )


def test_a_binding_rejects_a_transform_on_a_field_that_projects_nothing() -> None:
    """A transform on a non-field is a declaration that can never run."""
    with pytest.raises(ValueError, match="transforms name no projected field"):
        ProjectionBinding(target="device", object_type="d", transforms={"site_edge": "host"})


def test_an_edge_can_never_carry_a_transform() -> None:
    """An edge cites the key its target is stored under; altering it strands the link.

    This is the same defect class projection exists to remove -- one thing keyed two
    ways -- so it is closed off by construction rather than left to reviewer attention.
    """
    assert "site_edge" not in TRANSFORMABLE_FIELDS
    assert "parent_edge" not in TRANSFORMABLE_FIELDS


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "primary_ip4"),
        ("hostname_fallbacks", ("primary_ip6",)),
        ("display_name", "display"),
        ("device_hostname", "hostname"),
        ("mgmt_port", "port"),
        ("default_mgmt_port", 22),
        ("platform", "platform"),
        ("serial", "serial"),
        ("site_edge", "site"),
        ("native_tags", "tags"),
        ("secret_refs", {"ssh.username_ref": "custom_fields.username_ref"}),
        ("default_access_config", {"ssh": {"port": 22}}),
        ("maintenance_state", "status"),
        ("maintenance_state_map", {"offline": "disabled"}),
    ],
)
def test_site_binding_rejects_device_fields(field, value):
    """Every device-only declaration must fail instead of being silently ignored."""
    with pytest.raises(ValueError, match="ProjectionBinding"):
        ProjectionBinding(target="site", object_type="site", **{field: value})


@pytest.mark.parametrize("field", ["location", "parent_edge"])
def test_device_binding_rejects_site_fields(field):
    """A device cannot consume a site's location or parent relationship."""
    with pytest.raises(ValueError, match="site-only"):
        ProjectionBinding(target="device", object_type="device", **{field: field})


@pytest.mark.parametrize("field", sorted(TRANSFORMABLE_FIELDS))
def test_transform_requires_an_effective_source(field):
    """A transform on an unset source cannot produce a projected value."""
    target = "site" if field == "location" else "device"
    with pytest.raises(ValueError, match=f"transforms require a source: {field}"):
        ProjectionBinding(target=target, object_type="objects", transforms={field: ["lower"]})


@pytest.mark.parametrize("field", sorted(TRANSFORMABLE_FIELDS))
def test_transform_accepts_a_declared_source(field):
    """Every transformable column remains usable when its source is declared."""
    target = "site" if field == "location" else "device"
    binding = ProjectionBinding(
        target=target, object_type="objects", transforms={field: ["lower"]}, **{field: field}
    )
    assert binding.transforms[field] == ("lower",)


def test_hostname_transform_accepts_a_fallback_source():
    """Hostname fallback paths use the same transform chain as the primary path."""
    binding = ProjectionBinding(
        target="device",
        object_type="device",
        hostname_fallbacks=("primary_ip6",),
        transforms={"hostname": ["host"]},
    )
    assert binding.transforms["hostname"] == ("host",)


def test_port_default_does_not_supply_a_transform_source():
    """The host applies default ports after transforms, so a default cannot feed one."""
    with pytest.raises(ValueError, match="transforms require a source: mgmt_port"):
        ProjectionBinding(
            target="device",
            object_type="device",
            default_mgmt_port=22,
            transforms={"mgmt_port": ["strip"]},
        )
