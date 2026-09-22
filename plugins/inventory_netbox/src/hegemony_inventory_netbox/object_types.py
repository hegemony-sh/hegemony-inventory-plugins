# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The NetBox object families this provider can read, and how to address them.

This module is the catalogue's public face. The catalogue itself is no longer written
here: :mod:`.catalogue_generated` holds it, produced from a recorded NetBox schema by
the same deriver that reads a live instance, and :mod:`.overlay` holds the corrections
a schema cannot state. What is left here is what the rest of the plugin asks of a
catalogue however it was made -- look a family up, list the ids, hand the platform its
specs, hand the provider its fetch sources.

Reading everything NetBox holds is rarely what an operator wants: an interface table is
two orders of magnitude larger than a device table, and a rack elevation is not worth
syncing to drive a change. That choice belongs to the platform, which asks for it on the
provider's own form and then asks this provider only for the families it wants. So the
catalogue is the *space* of what can be read and every family in it is offered, with no
second say here: a config that could also narrow the space is how a ticked object type
came to sync nothing at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from hegemony_inventory_sdk import ObjectTypeSpec

from .catalogue_generated import OBJECT_TYPES
from .catalogue_model import (
    APP_GROUPS,
    Attr,
    NetBoxObjectType,
    qualified_object_type_id,
)

__all__ = [
    "ALL_OBJECT_TYPE_IDS",
    "APP_GROUPS",
    "BY_ID",
    "LEGACY_OBJECT_TYPE_IDS",
    "OBJECT_TYPES",
    "OBJECT_TYPE_IDS_BY_QUALIFIED",
    "Attr",
    "NetBoxObjectType",
    "object_type_specs",
    "qualified_object_type_id",
    "sources_for",
]

BY_ID: Mapping[str, NetBoxObjectType] = {entry.id: entry for entry in OBJECT_TYPES}

#: Every catalogue id, sorted, for error messages and documentation.
ALL_OBJECT_TYPE_IDS: tuple[str, ...] = tuple(sorted(BY_ID))

#: Registered id -> app-qualified id, for every family in the catalogue.
#:
#: Most families are named after their endpoint already (``circuits.circuit_groups``),
#: because that needs no choosing. Fifty are not: they carry bare singulars chosen by
#: hand before the deriver existed (``interface``, ``vm_interface``, ``ip_prefix``), and
#: :data:`~.overlay.REGISTERED_IDS` is where that choice is frozen.
#:
#: This table is the whole distance between the two spellings, and it exists to be
#: *used* rather than admired. An object-type id is not an internal detail -- it is
#: stored in ``inventory_provider_configs.object_types``, in the ``object_type`` of every
#: synced row, and inside the option sources of user-authored flow definitions -- so
#: adopting the derived ids means migrating that data and accepting both spellings while
#: it happens. Publishing the mapping here is what makes that migration mechanical.
LEGACY_OBJECT_TYPE_IDS: Mapping[str, str] = {entry.id: entry.qualified_id for entry in OBJECT_TYPES}

#: The reverse: app-qualified id -> the id this plugin registers today.
OBJECT_TYPE_IDS_BY_QUALIFIED: Mapping[str, str] = {
    qualified: legacy for legacy, qualified in LEGACY_OBJECT_TYPE_IDS.items()
}


def object_type_specs() -> tuple[ObjectTypeSpec, ...]:
    """Every catalogue family as a platform ``ObjectTypeSpec``, for registration."""
    return tuple(entry.spec for entry in OBJECT_TYPES)


def sources_for(object_type_ids: Iterable[str]) -> dict[str, NetBoxObjectType]:
    """The catalogue entries for ``object_type_ids``, ignoring ids it does not know."""
    return {
        object_type: BY_ID[object_type] for object_type in object_type_ids if object_type in BY_ID
    }
