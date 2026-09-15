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

``EXTRA_ATTRIBUTES``
    Properties worth reading that the deriver's one-level rule does not reach.

``BINDINGS``
    Which families are the platform's devices and sites, and how to read one from a
    stored row. What used to be ``DEFAULT_FIELD_MAP``, said in the platform's terms.

``EXTERNAL_ID_KEYS``
    Which property identifies a family's rows, where that is not its NetBox id. One key
    for storing a row and for citing it, because two keys for one thing is the defect.

``KEEP_PROPERTIES``
    Properties the deriver's noise rules drop that one family genuinely wants.

``SKIP_ENDPOINTS``
    NetBox's own plumbing, which is not inventory.

``REGISTERED_IDS``
    The id this plugin registers for a family, where it differs from the derived one.
    A persistence contract rather than a preference -- see the table's own note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from hegemony_inventory_sdk import ProjectionBinding

from .catalogue_model import qualified_object_type_id
from .config import DEFAULT_STATUS_MAP

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
    #: Whether the *generic* spelling belongs in the browse table.
    #:
    #: Excluding the legacy spelling from the columns is not enough on its own: where
    #: the replaced field was a column, dropping it and not promoting its replacement
    #: leaves the table showing neither. It is a per-pair judgement rather than a rule,
    #: because some replacements are not column material -- an interface's MAC and a
    #: contact's groups are worth searching and not worth the screen space.
    column: bool = False


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
#:   attribute simply never matches;
#: * the generic spelling takes the column the legacy one used to hold, where the pair
#:   says so -- see :attr:`ReplacedField.column`.
#:
#: The middle two are the asymmetry the plugin README sets out. Declaring the pairs once
#: is what stops the four rules from drifting apart.
REPLACED_FIELDS: Mapping[str, tuple[ReplacedField, ...]] = MappingProxyType(
    {
        "/api/circuits/circuit-terminations/": (
            ReplacedField(
                "site",
                "termination",
                "site.slug",
                legacy_target="/api/dcim/sites/",
                column=True,
            ),
            ReplacedField(
                "provider_network",
                "termination",
                "provider_network.name",
                legacy_target="/api/circuits/provider-networks/",
            ),
        ),
        "/api/dcim/interfaces/": (ReplacedField("mac_address", "primary_mac_address"),),
        "/api/ipam/prefixes/": (
            ReplacedField(
                "site",
                "scope",
                "site.slug",
                legacy_target="/api/dcim/sites/",
                column=True,
            ),
        ),
        "/api/ipam/services/": (
            ReplacedField(
                "device",
                "parent",
                "device.name",
                legacy_target="/api/dcim/devices/",
                column=True,
            ),
            ReplacedField(
                "virtual_machine",
                "parent",
                "virtual_machine.name",
                legacy_target="/api/virtualization/virtual-machines/",
            ),
        ),
        "/api/tenancy/contacts/": (ReplacedField("group", "groups", "group.name"),),
        "/api/virtualization/clusters/": (
            ReplacedField(
                "site",
                "scope",
                "site.slug",
                legacy_target="/api/dcim/sites/",
                column=True,
            ),
        ),
        "/api/virtualization/interfaces/": (ReplacedField("mac_address", "primary_mac_address"),),
    }
)


#: Properties a family should carry that the deriver's own rules do not reach, by
#: endpoint.
#:
#: The deriver reads one level into a reference and stores its natural key, which is
#: right almost everywhere: a device's ``platform`` is its slug, its ``site`` its slug.
#: A vendor is two levels down -- ``device_type.manufacturer.name`` -- and no rule that
#: stops at one level can find it. The path is still the schema's own: ``BriefDeviceType``
#: declares ``manufacturer`` and ``BriefManufacturer`` declares ``name``, so this says
#: *read further*, not *trust me*.
EXTRA_ATTRIBUTES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "/api/dcim/devices/": MappingProxyType({"vendor": "device_type.manufacturer.name"}),
    }
)


#: Which families are the platform's devices and sites, and how to read one from a stored
#: row.
#:
#: This is the whole of what used to live in ``DEFAULT_FIELD_MAP`` and in the provider's
#: synthetic site-tree code, said once and in the platform's own terms. Paths here read a
#: *stored object's* attributes, not a NetBox payload: the family's own mapping has
#: already resolved the dotted paths, so ``primary_ip4`` is the address and not the
#: nested object.
#:
#: Regions carry a prefix because a NetBox slug is unique per model and not across them:
#: a region and a site may both be ``gondor``, and the platform keys a site on
#: ``(provider_id, external_id)``. Sites do not, so a site row keeps exactly the key the
#: provider's device/site contract already stored it under and an upgrade re-uses the row
#: rather than making a second one.
BINDINGS: Mapping[str, ProjectionBinding] = MappingProxyType(
    {
        "/api/dcim/devices/": ProjectionBinding(
            target="device",
            object_type="device",
            name="name",
            hostname="primary_ip4",
            vendor="vendor",
            model="device_type",
            platform="platform",
            serial="serial",
            site_edge="site",
            maintenance_state="status",
            maintenance_state_map=DEFAULT_STATUS_MAP,
            secret_refs={
                "ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
                "ssh.password_ref": "custom_fields.hegemony_ssh_password_ref",
                "ssh.private_key_ref": "custom_fields.hegemony_ssh_private_key_ref",
                "enable.password_ref": "custom_fields.hegemony_enable_password_ref",
            },
            tags={"role": "role"},
        ),
        "/api/dcim/sites/": ProjectionBinding(
            target="site",
            object_type="site",
            name="name",
            parent_edge="region",
        ),
        "/api/dcim/regions/": ProjectionBinding(
            target="site",
            object_type="region",
            name="name",
            parent_edge="parent",
            external_id_prefix="region:",
        ),
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


#: The property that identifies a family's rows, where it is not the NetBox id.
#:
#: One key, used for two things that must agree: the ``external_id`` a row is *stored*
#: under, and the value an edge *cites* to reach it. Storing a site under its numeric id
#: while every edge into it cites its slug is two keys for one thing, and two keys for
#: one thing is how a NetBox site came to exist as two platform rows in the first place.
#:
#: Sites are the one entry because the platform's site contract keys them on their slug,
#: which is what an operator recognises and what a site selector already holds. For every
#: other family the NetBox id is both.
EXTERNAL_ID_KEYS: Mapping[str, str] = MappingProxyType({"/api/dcim/sites/": "slug"})

#: The object-type id this plugin registers for a NetBox endpoint.
#:
#: The deriver names a family after its endpoint -- ``dcim.interfaces`` -- because that
#: needs no choosing and cannot collide. The ids below were chosen by hand before it
#: existed, and they are a **persistence contract**: an object-type id is stored in
#: ``inventory_provider_configs.object_types``, in the ``object_type`` of every synced
#: row, and inside the option sources of user-authored flow definitions. Renaming one
#: strands all three.
#:
#: So discovery *extends* what a provider offers rather than shadowing it: a family
#: named here keeps its id, and only a family absent from this table -- a later
#: release's, a NetBox plugin's -- arrives under the derived spelling. Freezing the
#: table here rather than reading it back off the generated catalogue is what makes
#: generation reproducible from a schema and this module alone.
REGISTERED_IDS: Mapping[str, str] = MappingProxyType(
    {
        "/api/circuits/circuit-terminations/": "circuit_termination",
        "/api/circuits/circuit-types/": "circuit_type",
        "/api/circuits/circuits/": "circuit",
        "/api/circuits/provider-networks/": "provider_network",
        "/api/circuits/providers/": "circuit_provider",
        "/api/dcim/cables/": "cable",
        "/api/dcim/console-ports/": "console_port",
        "/api/dcim/device-roles/": "device_role",
        "/api/dcim/device-types/": "device_type",
        "/api/dcim/devices/": "device",
        "/api/dcim/front-ports/": "front_port",
        "/api/dcim/interfaces/": "interface",
        "/api/dcim/inventory-items/": "inventory_item",
        "/api/dcim/locations/": "location",
        "/api/dcim/manufacturers/": "manufacturer",
        "/api/dcim/module-types/": "module_type",
        "/api/dcim/modules/": "module",
        "/api/dcim/platforms/": "platform",
        "/api/dcim/power-feeds/": "power_feed",
        "/api/dcim/power-outlets/": "power_outlet",
        "/api/dcim/power-panels/": "power_panel",
        "/api/dcim/power-ports/": "power_port",
        "/api/dcim/racks/": "rack",
        "/api/dcim/regions/": "region",
        "/api/dcim/rear-ports/": "rear_port",
        "/api/dcim/site-groups/": "site_group",
        "/api/dcim/sites/": "site",
        "/api/dcim/virtual-chassis/": "virtual_chassis",
        "/api/dcim/virtual-device-contexts/": "virtual_device_context",
        "/api/extras/config-contexts/": "config_context",
        "/api/extras/journal-entries/": "journal_entry",
        "/api/ipam/aggregates/": "aggregate",
        "/api/ipam/asns/": "asn",
        "/api/ipam/fhrp-groups/": "fhrp_group",
        "/api/ipam/ip-addresses/": "ip_address",
        "/api/ipam/ip-ranges/": "ip_range",
        "/api/ipam/prefixes/": "ip_prefix",
        "/api/ipam/rirs/": "rir",
        "/api/ipam/roles/": "ipam_role",
        "/api/ipam/route-targets/": "route_target",
        "/api/ipam/services/": "service",
        "/api/ipam/vlan-groups/": "vlan_group",
        "/api/ipam/vlans/": "vlan",
        "/api/ipam/vrfs/": "vrf",
        "/api/tenancy/contacts/": "contact",
        "/api/tenancy/tenant-groups/": "tenant_group",
        "/api/tenancy/tenants/": "tenant",
        "/api/virtualization/clusters/": "cluster",
        "/api/virtualization/interfaces/": "vm_interface",
        "/api/virtualization/virtual-machines/": "virtual_machine",
        "/api/vpn/l2vpns/": "l2vpn",
        "/api/vpn/tunnels/": "vpn_tunnel",
        "/api/wireless/wireless-lans/": "wireless_lan",
        "/api/wireless/wireless-links/": "wireless_link",
    }
)

#: Derived id -> registered id. Relationship edges cite object type ids rather than
#: endpoints, so re-keying them needs this spelling.
REGISTERED_IDS_BY_DERIVED: Mapping[str, str] = MappingProxyType(
    {
        qualified_object_type_id(endpoint): object_type
        for endpoint, object_type in REGISTERED_IDS.items()
    }
)


#: The property every other family is cited by.
#: What identifies a row when the overlay says nothing: NetBox's own primary key, which
#: every family has and which every reference to one carries.
DEFAULT_EXTERNAL_ID_KEY = "id"


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
    external_id_keys: Mapping[str, str] = field(default_factory=lambda: EXTERNAL_ID_KEYS)
    extra_attributes: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: EXTRA_ATTRIBUTES
    )
    bindings: Mapping[str, ProjectionBinding] = field(default_factory=lambda: BINDINGS)
    keep_properties: frozenset[tuple[str, str]] = KEEP_PROPERTIES
    skip_endpoints: frozenset[str] = SKIP_ENDPOINTS


DEFAULT_OVERLAY = Overlay()


#: The site tree each ``site_tree_source`` choice produces.
#:
#: NetBox keeps two independent hierarchies above a site -- Region (geography) and Site
#: Group (function) -- and a platform site has one parent, so one has to be chosen. Both
#: are real families with real edges now, so the choice is which edge a site's parent
#: comes from and which second family is bound beside it, rather than which set of nodes
#: to invent. ``none`` binds sites alone and they arrive flat.
#:
#: Kept out of :data:`BINDINGS` deliberately: that is the default set the shipped
#: catalogue carries, and a catalogue offering both trees at once would describe a site
#: as having two parents.
SITE_TREE_BINDINGS: Mapping[str, tuple[str, str, ProjectionBinding]] = MappingProxyType(
    {
        # site_tree_source -> (the site's parent edge, the endpoint bound beside it, how)
        "region": (
            "region",
            "/api/dcim/regions/",
            ProjectionBinding(
                target="site",
                object_type="region",
                name="name",
                parent_edge="parent",
                external_id_prefix="region:",
            ),
        ),
        "site_group": (
            "group",
            "/api/dcim/site-groups/",
            ProjectionBinding(
                target="site",
                object_type="site_group",
                name="name",
                parent_edge="parent",
                external_id_prefix="site-group:",
            ),
        ),
    }
)
