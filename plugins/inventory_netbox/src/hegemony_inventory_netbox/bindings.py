# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Projection bindings for one provider, built from that provider's own config.

A binding says which stored family becomes the platform's devices or sites, and which
attribute fills which column. The shipped catalogue carries a default set, and defaults
are all a catalogue can carry -- it describes NetBox in general, and these are choices an
operator makes per provider: which hierarchy becomes the site tree, which NetBox status
means "in maintenance", which custom field holds the SSH credential reference.

Before discovery existed, those choices were read straight from the config every time a
device was mapped. Reading devices as objects moves the mapping to the platform, so the
choices have to travel with the binding or they stop being honoured -- silently, which is
the worst way for a credential mapping to stop working. This module is where they are
applied.

"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from hegemony_inventory_sdk import ProjectionBinding

from .config import NetBoxProviderConfig
from .overlay import BINDINGS, SITE_TREE_BINDINGS

__all__ = ["bindings_for"]

_DEVICE_ENDPOINT = "/api/dcim/devices/"
_SITE_ENDPOINT = "/api/dcim/sites/"

#: ``field_map`` keys whose value is a path into the device's stored attributes, and the
#: binding field each one fills. These are the entries an operator can still remap.
_SECRET_REF_KEYS: Mapping[str, str] = {
    "access_config.ssh.username_ref": "ssh.username_ref",
    "access_config.ssh.password_ref": "ssh.password_ref",
    "access_config.ssh.private_key_ref": "ssh.private_key_ref",
    "access_config.enable.password_ref": "enable.password_ref",
}


def _device_binding(config: NetBoxProviderConfig) -> ProjectionBinding:
    """The device binding with this provider's choices applied."""
    field_map = config.field_map or {}
    default = BINDINGS[_DEVICE_ENDPOINT]
    # Overlaid per key, never replaced as a whole: a config that remaps only the SSH
    # username keeps the default password, private-key and enable paths, exactly as the
    # device read fell back per key before projection. Replacing the map would make a
    # one-line remap silently drop three credential references.
    secret_refs = {
        **default.secret_refs,
        **{
            target: path for key, target in _SECRET_REF_KEYS.items() if (path := field_map.get(key))
        },
    }
    return replace(
        default,
        # Statuses left out of the map stay active, which is what an empty map means.
        maintenance_state_map={"*": "active", **dict(config.status_map or {})},
        secret_refs=secret_refs,
        default_access_config=dict(config.default_access_config or {}),
        tag_list="tags",
        native_tags="tags",
        tag_prefix=config.tag_key_prefix,
    )


def bindings_for(
    config: NetBoxProviderConfig, *, served: Iterable[str] = ()
) -> dict[str, ProjectionBinding]:
    """Every binding this provider should send, keyed by the endpoint it belongs to.

    ``served`` is the set of endpoints the instance actually serves. A binding for a
    family that is not there would ask the platform to project from rows which never
    arrive, so the site tree simply flattens instead -- which is the honest outcome for a
    NetBox whose regions were trimmed away.
    """
    available = set(served)
    composed: dict[str, ProjectionBinding] = {_DEVICE_ENDPOINT: _device_binding(config)}

    parent_edge: str | None = None
    tree = SITE_TREE_BINDINGS.get(config.site_tree_source)
    if tree is not None:
        edge, endpoint, binding = tree
        if not available or endpoint in available:
            composed[endpoint] = binding
            parent_edge = edge

    # No tree, or a tree this instance does not serve: sites arrive flat rather than
    # parented to rows nobody stored.
    composed[_SITE_ENDPOINT] = replace(BINDINGS[_SITE_ENDPOINT], parent_edge=parent_edge)
    return composed
