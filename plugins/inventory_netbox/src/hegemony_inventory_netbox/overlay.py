# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The editorial surface: everything a NetBox schema cannot tell you.

:mod:`.derive` reads a NetBox OpenAPI document and produces object types from it
mechanically. Most of what the hand-written catalogue says is recoverable that way --
endpoints, attributes, types, nullability, dotted paths, enum sets, grouping. What is
left over is this module, and keeping it small is the point: every entry here is a claim
the schema does not make, written down once with the reason it is needed.

The overlay is keyed by *endpoint*, never by object-type id, so it survives a renaming
of the ids and can be read against NetBox's own API reference. An endpoint absent from
every table below is derived with no correction at all, which is the case for most of
them.

Five kinds of correction live here, and nothing else belongs:

``NATURAL_KEYS``
    Which property of a referenced object to read. NetBox does not mark natural keys, so
    a reference to a site could reasonably store its name, its slug or its numeric id.

``UNTYPED_PATHS`` / ``UNTYPED_OBJECTS``
    NetBox's generic (polymorphic) references carry no type in the schema at all -- see
    the module docstring of :mod:`.derive`. The deriver cannot guess what to read from
    them, so it is told: a path to read one property, or a declaration that the whole
    object is worth keeping. The same untyped shape is also how NetBox renders free-form
    JSON, which is why the two cases have to be told apart by hand.

``REPLACED_FIELDS``
    Fields one supported NetBox release replaced with a generic equivalent. A schema
    describes one release; the shipped catalogue has to serve the supported range, so
    the pairs are declared rather than derived. This one table drives three rules --
    which spelling is synthesised, which is kept out of the browse columns, and which
    are searchable -- so the three cannot disagree.

``PRESENTATION``
    Browse columns and search fields where the derived default is poor.

``EDGE_KEYS``
    Which property identifies a family when something points at it, where that is not
    its NetBox id.

``KEEP_PROPERTIES``
    Properties the deriver's noise rules drop that one family genuinely wants.

``SKIP_ENDPOINTS``
    NetBox's own plumbing, which is not inventory.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

#: Referenced-object property to read, by the schema component naming that object.
#:
#: The deriver's own rule -- the first required property that is neither API metadata
#: nor private -- covers most references. These are the ones it cannot reach:
#:
#: * The ``slug`` group: the platform keys sites on their slug, and the catalogue has
#:   always stored slugs for the families an operator filters by. ``name`` would be the
#:   rule's answer because NetBox requires both.
#: * ``BriefVLAN``: a VLAN's identity is its numeric id on the wire, not its name.
NATURAL_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "BriefDeviceRole": "slug",
        "BriefPlatform": "slug",
        "BriefRackRole": "slug",
        "BriefRole": "slug",
        "BriefSite": "slug",
        "BriefTenant": "slug",
        "BriefVLAN": "vid",
    }
)

#: What to read from a generic reference, by ``endpoint`` and property name.
#:
#: NetBox serialises these as a nested object at runtime but describes them in the
#: schema as ``{"nullable": true, "readOnly": true}`` -- no type, no ``$ref``, nothing
#: to resolve. Each entry is a decision about which property of whatever was pointed at
#: is worth storing, and the choice follows from how open the target set is:
#:
#: * ``name`` where every model the field can hold has one -- a scope is a site, region,
#:   site group or location; an interface assignment is an interface of some kind.
#: * ``display`` where it can hold anything at all. A journal entry or a tag can be
#:   attached to a prefix or an IP address, neither of which has a name, and ``display``
#:   is the one label every NetBox object carries.
#:
#: Between them these cover every generic reference a stock NetBox 4.3 serves, so a
#: warning from the deriver means a shape this table has not seen -- a newer release, or
#: a plugin's own model.
UNTYPED_PATHS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("/api/circuits/circuit-group-assignments/", "member"): "member.display",
        ("/api/circuits/circuit-terminations/", "termination"): "termination.name",
        ("/api/dcim/cable-terminations/", "termination"): "termination.name",
        ("/api/dcim/inventory-item-templates/", "component"): "component.name",
        ("/api/dcim/inventory-items/", "component"): "component.name",
        ("/api/dcim/mac-addresses/", "assigned_object"): "assigned_object.name",
        ("/api/extras/journal-entries/", "assigned_object"): "assigned_object.display",
        ("/api/extras/tagged-objects/", "object"): "object.display",
        ("/api/ipam/fhrp-group-assignments/", "interface"): "interface.name",
        ("/api/ipam/ip-addresses/", "assigned_object"): "assigned_object.name",
        ("/api/ipam/prefixes/", "scope"): "scope.name",
        ("/api/ipam/services/", "parent"): "parent.display",
        ("/api/ipam/vlan-groups/", "scope"): "scope.name",
        ("/api/virtualization/clusters/", "scope"): "scope.name",
        ("/api/vpn/l2vpn-terminations/", "assigned_object"): "assigned_object.name",
        ("/api/vpn/tunnel-terminations/", "termination"): "termination.name",
        ("/api/wireless/wireless-lans/", "scope"): "scope.name",
    }
)

#: Untyped properties to store whole, by ``endpoint`` and property name.
#:
#: These share their schema shape with the generic references above and are not
#: references at all -- they are free-form JSON documents, and the useful thing to do
#: with one is keep it.
UNTYPED_OBJECTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/dcim/devices/", "config_context"),
        ("/api/dcim/devices/", "local_context_data"),
        ("/api/dcim/module-types/", "attributes"),
        ("/api/dcim/module-type-profiles/", "schema"),
        ("/api/extras/config-contexts/", "data"),
        ("/api/extras/config-templates/", "environment_params"),
        ("/api/virtualization/virtual-machines/", "config_context"),
        ("/api/virtualization/virtual-machines/", "local_context_data"),
    }
)


@dataclass(frozen=True, slots=True)
class ReplacedField:
    """One field a supported NetBox release replaced with a generic equivalent."""

    #: The pre-rename spelling.
    legacy: str
    #: What replaced it.
    generic: str
    #: Where to read the legacy spelling, for a release that serves it. ``None`` means
    #: the recorded schema still serves the field itself, so nothing is synthesised --
    #: the pair is declared only so both spellings stay searchable.
    legacy_path: str | None = None
    legacy_type: str = "string"
    #: Endpoint of the family the legacy spelling points at, when it is a reference.
    #: Without it a release serving the legacy field loses the edge as well as the name.
    legacy_target: str | None = None


#: Every field NetBox replaced, by endpoint.
#:
#: NetBox 4.2 and 4.3 replaced each of these with a generic equivalent, and this plugin
#: supports both sides of the rename. A schema describes one release, so the pairs are
#: declared here and three rules follow from them:
#:
#: * the legacy spelling is mapped, and its edge emitted, on a release that serves it;
#: * no browse column ever names it, because a column costs screen space whether or not
#:   anything lands in it;
#: * both spellings stay searchable, because a search field costs nothing and an absent
#:   attribute simply never matches.
#:
#: The last two are the asymmetry the plugin README sets out. Declaring the pairs once
#: is what stops the three rules from drifting apart.
REPLACED_FIELDS: Mapping[str, tuple[ReplacedField, ...]] = MappingProxyType(
    {
        "/api/circuits/circuit-terminations/": (
            ReplacedField("site", "termination", "site.slug", legacy_target="/api/dcim/sites/"),
            ReplacedField(
                "provider_network",
                "termination",
                "provider_network.name",
                legacy_target="/api/circuits/provider-networks/",
            ),
        ),
        "/api/dcim/interfaces/": (ReplacedField("mac_address", "primary_mac_address"),),
        "/api/ipam/prefixes/": (
            ReplacedField("site", "scope", "site.slug", legacy_target="/api/dcim/sites/"),
        ),
        "/api/ipam/services/": (
            ReplacedField("device", "parent", "device.name", legacy_target="/api/dcim/devices/"),
            ReplacedField(
                "virtual_machine",
                "parent",
                "virtual_machine.name",
                legacy_target="/api/virtualization/virtual-machines/",
            ),
        ),
        "/api/tenancy/contacts/": (ReplacedField("group", "groups", "group.name"),),
        "/api/virtualization/clusters/": (
            ReplacedField("site", "scope", "site.slug", legacy_target="/api/dcim/sites/"),
        ),
        "/api/virtualization/interfaces/": (ReplacedField("mac_address", "primary_mac_address"),),
    }
)


@dataclass(frozen=True, slots=True)
class Presentation:
    """Browse columns and search fields for one family, where the default is poor."""

    columns: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    icon: str | None = None
    display_name: str | None = None
    plural: str | None = None
    description: str | None = None


#: Property to cite when pointing *at* a family, where it is not the NetBox id.
#:
#: An attribute and an edge want different things from the same reference. The attribute
#: stores what a person reads -- a site's slug, a VLAN's number -- while an edge has to
#: cite the target's **external id**, because that is the key the platform stored the
#: target under and the only thing that will resolve back to it. For every NetBox family
#: that is the numeric ``id``, which is why this table has one entry: the platform
#: materialises sites through the site contract, which keys them on their slug.
EDGE_KEYS: Mapping[str, str] = MappingProxyType({"/api/dcim/sites/": "slug"})

#: The property every other family is cited by.
DEFAULT_EDGE_KEY = "id"


#: Django content-type label by endpoint, where the deriver's rule cannot reach it.
#:
#: A NetBox OpenAPI document names no content types at all, so the label a custom field
#: is attached to has to be rebuilt from the endpoint. The rule -- app plus the
#: singularised segment -- is right for every endpoint a stock 4.3 serves; this table is
#: here because a NetBox plugin is free to name its endpoints anything.
CONTENT_TYPES: Mapping[str, str] = MappingProxyType({})


#: Properties to keep although a noise rule would drop them, by endpoint and name.
#:
#: The deriver discards NetBox's denormalised child counters: they are recomputed on
#: every read and say how many other objects point at this one rather than anything
#: about it. A virtual chassis is the exception -- how many members it has is the fact
#: an operator wants from the list, not a property of its members.
KEEP_PROPERTIES: frozenset[tuple[str, str]] = frozenset(
    {("/api/dcim/virtual-chassis/", "member_count")}
)


#: NetBox's own plumbing: endpoints that exist on every instance and are never
#: inventory. Anything not listed is offered, because deciding what an operator may sync
#: is the platform's job and not this plugin's -- see :mod:`.object_types`.
SKIP_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/api/core/data-files/",
        "/api/core/data-sources/",
        "/api/core/jobs/",
        "/api/core/object-changes/",
        "/api/extras/bookmarks/",
        "/api/extras/custom-field-choice-sets/",
        "/api/extras/custom-fields/",
        "/api/extras/custom-links/",
        "/api/extras/event-rules/",
        "/api/extras/export-templates/",
        "/api/extras/image-attachments/",
        "/api/extras/notification-groups/",
        "/api/extras/notifications/",
        "/api/extras/object-changes/",
        "/api/extras/saved-filters/",
        "/api/extras/scripts/",
        "/api/extras/subscriptions/",
        "/api/extras/table-configs/",
        "/api/extras/webhooks/",
        "/api/users/config/",
        "/api/users/groups/",
        "/api/users/permissions/",
        "/api/users/tokens/",
        "/api/users/users/",
    }
)


@dataclass(frozen=True, slots=True)
class Overlay:
    """Every correction applied on top of a derived catalogue."""

    natural_keys: Mapping[str, str] = field(default_factory=lambda: NATURAL_KEYS)
    untyped_paths: Mapping[tuple[str, str], str] = field(default_factory=lambda: UNTYPED_PATHS)
    untyped_objects: frozenset[tuple[str, str]] = UNTYPED_OBJECTS
    replaced_fields: Mapping[str, tuple[ReplacedField, ...]] = field(
        default_factory=lambda: REPLACED_FIELDS
    )
    presentation: Mapping[str, Presentation] = field(default_factory=dict)
    content_types: Mapping[str, str] = field(default_factory=lambda: CONTENT_TYPES)
    edge_keys: Mapping[str, str] = field(default_factory=lambda: EDGE_KEYS)
    keep_properties: frozenset[tuple[str, str]] = KEEP_PROPERTIES
    skip_endpoints: frozenset[str] = SKIP_ENDPOINTS


DEFAULT_OVERLAY = Overlay()
