# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A provider's own config still shapes how its devices and sites are read.

Before discovery, every one of these choices was applied while mapping each device, from
the config in the provider's hand. Reading devices as objects moves the mapping to the
platform, so each choice has to travel on the binding instead. A choice that stopped
being honoured would not fail: it would quietly produce devices with the wrong
credentials, or a flat site list, and nothing would say so. That is what these pin.
"""

from __future__ import annotations

import pytest

from hegemony_inventory_netbox.bindings import bindings_for, unsupported_field_map_keys
from hegemony_inventory_netbox.config import DEFAULT_FIELD_MAP, NetBoxProviderConfig
from hegemony_inventory_sdk import ProjectionBinding

_BASE = {"url": "https://netbox.example", "token_ref": "ref"}


def _config(**overrides: object) -> NetBoxProviderConfig:
    return NetBoxProviderConfig.model_validate({**_BASE, **overrides})


def _device(config: NetBoxProviderConfig) -> ProjectionBinding:
    return bindings_for(config)["/api/dcim/devices/"]


@pytest.mark.parametrize(
    ("source", "families", "parent_edge"),
    [
        ("region", {"device", "region", "site"}, "region"),
        ("site_group", {"device", "site", "site_group"}, "group"),
        ("none", {"device", "site"}, None),
    ],
)
def test_site_tree_source_picks_which_hierarchy_is_bound(
    source: str, families: set[str], parent_edge: str | None
) -> None:
    """NetBox has two hierarchies above a site and the platform has one parent.

    The choice used to decide which set of nodes the provider invented. It now decides
    which family is bound beside sites and which edge a site's parent comes from -- real
    rows either way, which is why a region can finally be browsed like anything else.
    """
    composed = bindings_for(_config(site_tree_source=source))

    assert {binding.object_type for binding in composed.values()} == families
    assert composed["/api/dcim/sites/"].parent_edge == parent_edge


def test_a_tree_this_instance_does_not_serve_flattens_rather_than_dangles() -> None:
    """Binding a family the instance does not serve would project from nothing.

    A NetBox with its region endpoint trimmed away should give a flat site list, not a
    site tree whose every parent names a row that was never stored.
    """
    composed = bindings_for(_config(), served={"/api/dcim/devices/", "/api/dcim/sites/"})

    assert {binding.object_type for binding in composed.values()} == {"device", "site"}
    assert composed["/api/dcim/sites/"].parent_edge is None


def test_a_remapped_credential_path_reaches_the_binding() -> None:
    """The one that matters most: an operator's own custom field for SSH credentials.

    Losing this would not break a sync. It would produce devices whose credential
    references point at a field nobody populates, and the first anyone would know is a
    run that cannot authenticate.
    """
    config = _config(
        field_map={
            **DEFAULT_FIELD_MAP,
            "access_config.ssh.username_ref": "custom_fields.my_ssh_user",
        }
    )

    assert _device(config).secret_refs["ssh.username_ref"] == "custom_fields.my_ssh_user"
    # The three the operator left alone still come through.
    assert (
        _device(config).secret_refs["ssh.password_ref"] == "custom_fields.hegemony_ssh_password_ref"
    )


def test_status_map_and_tag_prefix_reach_the_binding() -> None:
    """Which NetBox status means maintenance, and how a NetBox tag is namespaced."""
    binding = _device(_config(status_map={"planned": "maintenance"}, tag_key_prefix="nb:"))

    assert binding.maintenance_state_map == {"planned": "maintenance"}
    assert (binding.tag_list, binding.tag_prefix) == ("tags", "nb:")


def test_an_empty_status_map_means_every_status_is_active() -> None:
    """An operator who clears the map is saying "never infer maintenance from status".

    Falling back to the shipped default here would quietly override that, which is the
    opposite of what clearing a setting means.
    """
    assert _device(_config(status_map={})).maintenance_state_map == {}


def test_provider_defaults_travel_as_literals() -> None:
    """There is no path in a device record to read a provider-wide setting from."""
    binding = _device(_config(default_access_config={"ssh": {"port": 2222}}))

    assert binding.default_access_config == {"ssh": {"port": 2222}}


def test_a_field_map_entry_a_binding_cannot_honour_is_named() -> None:
    """Told, not dropped.

    ``vendor`` was a two-level reach into NetBox's raw payload; the stored object fixes
    it. An operator who remapped it is going to get the catalogue's value regardless, and
    should hear that from discovery rather than from a column that looks wrong.
    """
    config = _config(field_map={**DEFAULT_FIELD_MAP, "vendor": "custom_fields.made_by"})

    assert unsupported_field_map_keys(config) == ("vendor",)


def test_an_untouched_field_map_warns_about_nothing() -> None:
    """Warning about a default would mean every provider warns, which is no warning."""
    assert unsupported_field_map_keys(_config()) == ()
    assert unsupported_field_map_keys(_config(field_map=dict(DEFAULT_FIELD_MAP))) == ()
