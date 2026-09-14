# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The NetBox object families this provider can read, as one declarative catalogue.

Two things have to agree for an object type to work end to end: the provider needs an
API endpoint and a record -> descriptor mapping to read it with, and the platform needs
an ``ObjectTypeSpec`` to list, search and render it with. Declaring those separately is
how they drift, so both are derived from the entries here -- ``register()`` turns each
into a spec, and the provider turns each into a fetch source. Adding a NetBox family is
one entry in :data:`OBJECT_TYPES` and nothing else.

Reading everything NetBox holds is rarely what an operator wants: an interface table is
two orders of magnitude larger than a device table, and a rack elevation is not worth
syncing to drive a change. That choice belongs to the platform, which asks for it on the
provider's own form and then asks this provider only for the families it wants. So the
catalogue is the *space* of what can be read and every family in it is offered, with no
second say here: a config that could also narrow the space is how a ticked object type
came to sync nothing at all.

Field paths are dotted (``status.value`` reads NetBox's rendered choice object). A path
that a NetBox version does not serve yields ``None`` rather than an error, which is how
one catalogue serves several NetBox releases: where 4.2 renamed something, both
spellings are mapped and the absent one is simply null.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hegemony_inventory_sdk import (
    ObjectFieldMapping,
    ObjectTypeRelationshipSpec,
    ObjectTypeSpec,
    ObjectTypeUIHints,
    RelationshipMapping,
    ResourceType,
)


@dataclass(frozen=True, slots=True)
class Attr:
    """One stored attribute: where to read it, and what shape it has."""

    path: str
    json_type: str = "string"
    nullable: bool = True

    @property
    def schema(self) -> dict[str, Any]:
        """This attribute as a JSON Schema property."""
        if self.nullable:
            return {"type": [self.json_type, "null"]}
        return {"type": self.json_type}


def _text(path: str, *, nullable: bool = True) -> Attr:
    return Attr(path, "string", nullable)


def _int(path: str, *, nullable: bool = True) -> Attr:
    return Attr(path, "integer", nullable)


def _number(path: str, *, nullable: bool = True) -> Attr:
    return Attr(path, "number", nullable)


def _bool(path: str, *, nullable: bool = True) -> Attr:
    return Attr(path, "boolean", nullable)


def _list(path: str) -> Attr:
    return Attr(path, "array")


def _json(path: str) -> Attr:
    return Attr(path, "object")


def _rel(rel: str, target: str, source: str) -> RelationshipMapping:
    return RelationshipMapping(rel=rel, target_object_type=target, source=source)


#: NetBox app label -> the name an operator sees it under. NetBox's own apps are the
#: division its own navigation uses, so grouping the catalogue any other way would
#: invent a second vocabulary for the same fifty things.
APP_GROUPS: Mapping[str, str] = {
    "circuits": "Circuits",
    "dcim": "DCIM",
    "extras": "Extras",
    "ipam": "IPAM",
    "tenancy": "Tenancy",
    "virtualization": "Virtualization",
    "vpn": "VPN",
    "wireless": "Wireless",
}


@dataclass(frozen=True, slots=True)
class NetBoxObjectType:
    """One NetBox family: how to read it, and how the platform should present it."""

    id: str
    endpoint: str
    display_name: str
    plural: str
    description: str
    attributes: Mapping[str, Attr]
    #: NetBox app the endpoint belongs to. It is also the family grouping the platform
    #: presents the fifty types under, because NetBox's own apps are the division an
    #: operator already thinks in -- see :data:`APP_GROUPS`.
    app: str = "dcim"
    #: Record path whose value becomes the object's name. ``display`` is NetBox's own
    #: human label and exists on every object, so it is the safe default.
    name_field: str = "display"
    display_name_field: str | None = "display"
    #: Record path whose value becomes the external id. NetBox's numeric primary key is
    #: the only identifier every family has, and it is what related objects cite.
    identity: str = "id"
    #: The natural key a human would use; documentation for readers, not a lookup key.
    identity_keys: tuple[str, ...] = ("id",)
    columns: tuple[str, ...] = ("name",)
    search_fields: tuple[str, ...] = ("name",)
    icon: str | None = None
    relationships: tuple[RelationshipMapping, ...] = ()
    #: Extra NetBox filter always applied to this family, on top of any configured one.
    base_query: str = ""

    @property
    def mapping(self) -> ObjectFieldMapping:
        """The declarative record -> ``ObjectDescriptor`` mapping for this family."""
        return ObjectFieldMapping(
            object_type=self.id,
            identity=self.identity,
            name_field=self.name_field,
            display_name_field=self.display_name_field,
            field_map={name: attr.path for name, attr in self.attributes.items()},
            relationships=self.relationships,
        )

    @property
    def spec(self) -> ObjectTypeSpec:
        """The platform-facing spec: schema, identity, relationships and UI hints."""
        return ObjectTypeSpec(
            id=self.id,
            display_name=self.display_name,
            plural=self.plural,
            description=self.description,
            group=APP_GROUPS.get(self.app, self.app.upper()),
            field_schema={
                "type": "object",
                "properties": {name: attr.schema for name, attr in self.attributes.items()},
            },
            identity_keys=self.identity_keys,
            relationships=tuple(
                ObjectTypeRelationshipSpec(
                    rel=relationship.rel,
                    target_object_type=relationship.target_object_type,
                )
                for relationship in self.relationships
            ),
            ui=ObjectTypeUIHints(
                icon=self.icon,
                columns=self.columns,
                search_fields=self.search_fields,
            ),
        )


# ── The catalogue ─────────────────────────────────────────────────────────────
#
# Grouped by NetBox app, in the order a reader of NetBox's own navigation would
# expect. Deliberately absent: regions and site groups, which this provider already
# projects onto the platform's site tree (see ``site_tree_source``) and would
# otherwise exist twice; bays and cable terminations, which are structural
# placeholders rather than things an operator targets; and NetBox's own plumbing
# (webhooks, scripts, jobs, object changes), which is not inventory.

_TENANCY: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="tenant",
        app="tenancy",
        endpoint="/api/tenancy/tenants/",
        display_name="Tenant",
        plural="Tenants",
        description="Tenants: who owns or consumes a piece of the estate.",
        name_field="name",
        identity_keys=("slug",),
        icon="building",
        columns=("name", "slug", "group", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "group": _text("group.name"),
            "description": _text("description"),
        },
        relationships=(_rel("tenant_group", "tenant_group", "group.id"),),
    ),
    NetBoxObjectType(
        id="tenant_group",
        app="tenancy",
        endpoint="/api/tenancy/tenant-groups/",
        display_name="Tenant Group",
        plural="Tenant Groups",
        description="Nested grouping of tenants.",
        name_field="name",
        identity_keys=("slug",),
        icon="building",
        columns=("name", "slug", "parent", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "parent": _text("parent.name"),
            "description": _text("description"),
        },
        relationships=(_rel("parent", "tenant_group", "parent.id"),),
    ),
    NetBoxObjectType(
        id=ResourceType.CONTACT.value,
        app="tenancy",
        endpoint="/api/tenancy/contacts/",
        display_name="Contact",
        plural="Contacts",
        description="People and teams responsible for parts of the estate.",
        name_field="name",
        identity_keys=("name",),
        icon="user",
        columns=("name", "title", "email", "phone"),
        search_fields=("name", "email", "title", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "title": _text("title"),
            "phone": _text("phone"),
            "email": _text("email"),
            "address": _text("address"),
            "link": _text("link"),
            # NetBox 4.2 replaced Contact.group with a many-to-many `groups`; both are
            # mapped so one catalogue serves either release.
            "group": _text("group.name"),
            "groups": _list("groups"),
            "description": _text("description"),
        },
    ),
)

_DCIM: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="location",
        endpoint="/api/dcim/locations/",
        display_name="Location",
        plural="Locations",
        description="Sub-site locations: floors, rooms, suites.",
        name_field="name",
        identity_keys=("slug",),
        icon="map-pin",
        columns=("name", "site", "parent", "status"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "site": _text("site.slug"),
            "parent": _text("parent.name"),
            "status": _text("status.value"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel("parent", "location", "parent.id"),
        ),
    ),
    NetBoxObjectType(
        id="rack",
        endpoint="/api/dcim/racks/",
        display_name="Rack",
        plural="Racks",
        description="Racks and their physical identity.",
        name_field="name",
        identity_keys=("site", "name"),
        icon="server",
        columns=("name", "site", "status", "role", "u_height"),
        search_fields=("name", "serial", "asset_tag", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "status": _text("status.value"),
            "site": _text("site.slug"),
            "location": _text("location.name"),
            "role": _text("role.slug"),
            "tenant": _text("tenant.slug"),
            "serial": _text("serial"),
            "asset_tag": _text("asset_tag"),
            "facility_id": _text("facility_id"),
            "u_height": _int("u_height"),
            "width": _int("width.value"),
            "description": _text("description"),
        },
        relationships=(
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel("location", "location", "location.id"),
        ),
    ),
    NetBoxObjectType(
        id="manufacturer",
        endpoint="/api/dcim/manufacturers/",
        display_name="Manufacturer",
        plural="Manufacturers",
        description="Hardware vendors.",
        name_field="name",
        identity_keys=("slug",),
        icon="factory",
        columns=("name", "slug", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="device_type",
        endpoint="/api/dcim/device-types/",
        display_name="Device Type",
        plural="Device Types",
        description="Hardware models, as NetBox models them.",
        name_field="model",
        identity_keys=("manufacturer", "slug"),
        icon="cpu",
        columns=("name", "manufacturer", "part_number", "u_height"),
        search_fields=("name", "part_number", "description"),
        attributes={
            "model": _text("model", nullable=False),
            "slug": _text("slug"),
            "manufacturer": _text("manufacturer.name"),
            "part_number": _text("part_number"),
            "u_height": _number("u_height"),
            "is_full_depth": _bool("is_full_depth"),
            "description": _text("description"),
        },
        relationships=(_rel("manufacturer", "manufacturer", "manufacturer.id"),),
    ),
    NetBoxObjectType(
        id="device_role",
        endpoint="/api/dcim/device-roles/",
        display_name="Device Role",
        plural="Device Roles",
        description="The role a device plays, which is how fleets get scoped.",
        name_field="name",
        identity_keys=("slug",),
        icon="tag",
        columns=("name", "slug", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "color": _text("color"),
            "vm_role": _bool("vm_role"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="platform",
        endpoint="/api/dcim/platforms/",
        display_name="Platform",
        plural="Platforms",
        description="Operating system platforms, which drive connection handlers.",
        name_field="name",
        identity_keys=("slug",),
        icon="terminal",
        columns=("name", "slug", "manufacturer", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "manufacturer": _text("manufacturer.name"),
            "description": _text("description"),
        },
        relationships=(_rel("manufacturer", "manufacturer", "manufacturer.id"),),
    ),
    NetBoxObjectType(
        id="interface",
        endpoint="/api/dcim/interfaces/",
        display_name="Interface",
        plural="Interfaces",
        description="Device interfaces. The largest table in most NetBox instances.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="ethernet-port",
        columns=("name", "device", "type", "enabled", "mode"),
        search_fields=("name", "description", "mac_address"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "enabled": _bool("enabled"),
            "mtu": _int("mtu"),
            # NetBox 4.2 moved the MAC onto a related MACAddress object and left the old
            # field in place for a release; both spellings are mapped.
            "mac_address": _text("mac_address"),
            "primary_mac_address": _text("primary_mac_address.mac_address"),
            "mode": _text("mode.value"),
            "lag": _text("lag.name"),
            "untagged_vlan": _int("untagged_vlan.vid"),
            "mark_connected": _bool("mark_connected"),
            "description": _text("description"),
        },
        relationships=(
            _rel("device", ResourceType.DEVICE.value, "device.id"),
            _rel("lag", "interface", "lag.id"),
            _rel("untagged_vlan", ResourceType.VLAN.value, "untagged_vlan.id"),
        ),
    ),
    NetBoxObjectType(
        id="console_port",
        endpoint="/api/dcim/console-ports/",
        display_name="Console Port",
        plural="Console Ports",
        description="Console ports, which is how out-of-band access is modelled.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="terminal",
        columns=("name", "device", "type", "description"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "speed": _int("speed.value"),
            "mark_connected": _bool("mark_connected"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="power_port",
        endpoint="/api/dcim/power-ports/",
        display_name="Power Port",
        plural="Power Ports",
        description="Device power inlets and their draw.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="plug",
        columns=("name", "device", "type", "maximum_draw"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "maximum_draw": _int("maximum_draw"),
            "allocated_draw": _int("allocated_draw"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="power_outlet",
        endpoint="/api/dcim/power-outlets/",
        display_name="Power Outlet",
        plural="Power Outlets",
        description="Device power outlets and the inlet each is fed from.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="plug",
        columns=("name", "device", "type", "power_port"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "power_port": _text("power_port.name"),
            "feed_leg": _text("feed_leg.value"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="front_port",
        endpoint="/api/dcim/front-ports/",
        display_name="Front Port",
        plural="Front Ports",
        description="Patch-panel front ports and the rear port each maps to.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="ethernet-port",
        columns=("name", "device", "type", "rear_port"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "rear_port": _text("rear_port.name"),
            "rear_port_position": _int("rear_port_position"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="rear_port",
        endpoint="/api/dcim/rear-ports/",
        display_name="Rear Port",
        plural="Rear Ports",
        description="Patch-panel rear ports.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="ethernet-port",
        columns=("name", "device", "type", "positions"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "type": _text("type.value"),
            "positions": _int("positions"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="module",
        endpoint="/api/dcim/modules/",
        display_name="Module",
        plural="Modules",
        description="Field-replaceable modules installed in device bays.",
        identity_keys=("device", "module_bay"),
        icon="cpu",
        columns=("name", "device", "module_type", "serial", "status"),
        search_fields=("name", "serial", "asset_tag", "description"),
        attributes={
            "device": _text("device.name"),
            "module_bay": _text("module_bay.name"),
            "module_type": _text("module_type.model"),
            "status": _text("status.value"),
            "serial": _text("serial"),
            "asset_tag": _text("asset_tag"),
            "description": _text("description"),
        },
        relationships=(
            _rel("device", ResourceType.DEVICE.value, "device.id"),
            _rel("module_type", "module_type", "module_type.id"),
        ),
    ),
    NetBoxObjectType(
        id="module_type",
        endpoint="/api/dcim/module-types/",
        display_name="Module Type",
        plural="Module Types",
        description="Module models, as NetBox models them.",
        name_field="model",
        identity_keys=("manufacturer", "model"),
        icon="cpu",
        columns=("name", "manufacturer", "part_number", "description"),
        search_fields=("name", "part_number", "description"),
        attributes={
            "model": _text("model", nullable=False),
            "manufacturer": _text("manufacturer.name"),
            "part_number": _text("part_number"),
            "description": _text("description"),
        },
        relationships=(_rel("manufacturer", "manufacturer", "manufacturer.id"),),
    ),
    NetBoxObjectType(
        id="inventory_item",
        endpoint="/api/dcim/inventory-items/",
        display_name="Inventory Item",
        plural="Inventory Items",
        description="Parts inside a device: transceivers, line cards, disks.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="package",
        columns=("name", "device", "manufacturer", "serial", "discovered"),
        search_fields=("name", "serial", "asset_tag", "part_id", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "manufacturer": _text("manufacturer.name"),
            "part_id": _text("part_id"),
            "serial": _text("serial"),
            "asset_tag": _text("asset_tag"),
            "discovered": _bool("discovered"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
    NetBoxObjectType(
        id="cable",
        endpoint="/api/dcim/cables/",
        display_name="Cable",
        plural="Cables",
        description="Physical cabling. What LLDP is checked against.",
        identity_keys=("id",),
        icon="cable",
        columns=("name", "type", "status", "label"),
        search_fields=("label", "description"),
        attributes={
            "label": _text("label"),
            "type": _text("type.value"),
            "status": _text("status.value"),
            "a_terminations": _list("a_terminations"),
            "b_terminations": _list("b_terminations"),
            "length": _number("length"),
            "length_unit": _text("length_unit.value"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="power_panel",
        endpoint="/api/dcim/power-panels/",
        display_name="Power Panel",
        plural="Power Panels",
        description="Site power panels that feeds hang from.",
        name_field="name",
        identity_keys=("site", "name"),
        icon="zap",
        columns=("name", "site", "location", "description"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "site": _text("site.slug"),
            "location": _text("location.name"),
            "description": _text("description"),
        },
        relationships=(_rel("site", ResourceType.SITE.value, "site.slug"),),
    ),
    NetBoxObjectType(
        id="power_feed",
        endpoint="/api/dcim/power-feeds/",
        display_name="Power Feed",
        plural="Power Feeds",
        description="Power feeds from a panel to a rack, with their electrical spec.",
        name_field="name",
        identity_keys=("power_panel", "name"),
        icon="zap",
        columns=("name", "power_panel", "rack", "status", "voltage"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "power_panel": _text("power_panel.name"),
            "rack": _text("rack.name"),
            "status": _text("status.value"),
            "type": _text("type.value"),
            "supply": _text("supply.value"),
            "phase": _text("phase.value"),
            "voltage": _int("voltage"),
            "amperage": _int("amperage"),
            "max_utilization": _int("max_utilization"),
            "description": _text("description"),
        },
        relationships=(
            _rel("power_panel", "power_panel", "power_panel.id"),
            _rel("rack", "rack", "rack.id"),
        ),
    ),
    NetBoxObjectType(
        id="virtual_chassis",
        endpoint="/api/dcim/virtual-chassis/",
        display_name="Virtual Chassis",
        plural="Virtual Chassis",
        description="Stacks presented as one logical switch.",
        name_field="name",
        identity_keys=("name",),
        icon="layers",
        columns=("name", "domain", "master", "member_count"),
        search_fields=("name", "domain", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "domain": _text("domain"),
            "master": _text("master.name"),
            "member_count": _int("member_count"),
            "description": _text("description"),
        },
        relationships=(_rel("master", ResourceType.DEVICE.value, "master.id"),),
    ),
    NetBoxObjectType(
        id="virtual_device_context",
        endpoint="/api/dcim/virtual-device-contexts/",
        display_name="Virtual Device Context",
        plural="Virtual Device Contexts",
        description="Logical partitions of one physical device.",
        name_field="name",
        identity_keys=("device", "name"),
        icon="layers",
        columns=("name", "device", "identifier", "status"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "device": _text("device.name"),
            "identifier": _int("identifier"),
            "status": _text("status.value"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(_rel("device", ResourceType.DEVICE.value, "device.id"),),
    ),
)

_IPAM: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id=ResourceType.IP_PREFIX.value,
        app="ipam",
        endpoint="/api/ipam/prefixes/",
        display_name="IP Prefix",
        plural="IP Prefixes",
        description="IP prefixes / subnets from NetBox IPAM.",
        name_field="prefix",
        identity_keys=("prefix",),
        icon="network",
        columns=("name", "status", "vlan", "role", "site"),
        search_fields=("name", "description"),
        attributes={
            "prefix": _text("prefix", nullable=False),
            "status": _text("status.value"),
            "vlan": _int("vlan.vid"),
            "role": _text("role.slug"),
            # NetBox 4.2 replaced Prefix.site with a generic scope, which can be a site,
            # a region, a site group or a location. Both spellings are mapped: `site`
            # still resolves on a pre-4.2 NetBox and is simply absent after, and
            # `scope`/`scope_type` carry the newer shape -- with the type named, because
            # a bare slug labelled "site" would be a lie when the prefix is actually
            # scoped to a region.
            "site": _text("site.slug"),
            "scope": _text("scope.name"),
            "scope_type": _text("scope_type"),
            "vrf": _text("vrf.name"),
            "tenant": _text("tenant.slug"),
            "is_pool": _bool("is_pool"),
            "description": _text("description"),
        },
        relationships=(
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel(ResourceType.VLAN.value, ResourceType.VLAN.value, "vlan.id"),
            _rel("vrf", "vrf", "vrf.id"),
        ),
    ),
    NetBoxObjectType(
        id=ResourceType.IP_ADDRESS.value,
        app="ipam",
        endpoint="/api/ipam/ip-addresses/",
        display_name="IP Address",
        plural="IP Addresses",
        description="IP addresses from NetBox IPAM.",
        name_field="address",
        identity_keys=("address",),
        icon="network",
        columns=("name", "status", "dns_name", "description"),
        search_fields=("name", "dns_name"),
        attributes={
            "address": _text("address", nullable=False),
            "status": _text("status.value"),
            "role": _text("role.value"),
            "dns_name": _text("dns_name"),
            "vrf": _text("vrf.name"),
            "tenant": _text("tenant.slug"),
            # What the address is assigned to is polymorphic in NetBox (an interface, a
            # VM interface, an FHRP group), so it is stored as the type plus id rather
            # than as a relationship, which needs one known target type.
            "assigned_object_type": _text("assigned_object_type"),
            "assigned_object_id": _int("assigned_object_id"),
            "description": _text("description"),
        },
        relationships=(_rel("vrf", "vrf", "vrf.id"),),
    ),
    NetBoxObjectType(
        id=ResourceType.VLAN.value,
        app="ipam",
        endpoint="/api/ipam/vlans/",
        display_name="VLAN",
        plural="VLANs",
        description="VLANs from NetBox IPAM.",
        name_field="name",
        identity_keys=("vid", "site"),
        icon="layers",
        columns=("name", "vid", "status", "site", "role"),
        search_fields=("name",),
        attributes={
            "vid": _int("vid"),
            "name": _text("name", nullable=False),
            "status": _text("status.value"),
            "site": _text("site.slug"),
            "group": _text("group.name"),
            "role": _text("role.slug"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel("vlan_group", "vlan_group", "group.id"),
        ),
    ),
    NetBoxObjectType(
        id="ip_range",
        app="ipam",
        endpoint="/api/ipam/ip-ranges/",
        display_name="IP Range",
        plural="IP Ranges",
        description="Reserved address ranges, such as DHCP pools.",
        identity_keys=("start_address", "end_address"),
        icon="network",
        columns=("name", "status", "size", "vrf"),
        search_fields=("name", "description"),
        attributes={
            "start_address": _text("start_address", nullable=False),
            "end_address": _text("end_address", nullable=False),
            "size": _int("size"),
            "status": _text("status.value"),
            "role": _text("role.slug"),
            "vrf": _text("vrf.name"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(_rel("vrf", "vrf", "vrf.id"),),
    ),
    NetBoxObjectType(
        id="aggregate",
        app="ipam",
        endpoint="/api/ipam/aggregates/",
        display_name="Aggregate",
        plural="Aggregates",
        description="Top-level allocations the address plan hangs from.",
        name_field="prefix",
        identity_keys=("prefix",),
        icon="network",
        columns=("name", "rir", "tenant", "description"),
        search_fields=("name", "description"),
        attributes={
            "prefix": _text("prefix", nullable=False),
            "rir": _text("rir.name"),
            "tenant": _text("tenant.slug"),
            "date_added": _text("date_added"),
            "description": _text("description"),
        },
        relationships=(_rel("rir", "rir", "rir.id"),),
    ),
    NetBoxObjectType(
        id="rir",
        app="ipam",
        endpoint="/api/ipam/rirs/",
        display_name="RIR",
        plural="RIRs",
        description="Regional Internet Registries and private address spaces.",
        name_field="name",
        identity_keys=("slug",),
        icon="globe",
        columns=("name", "slug", "is_private", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "is_private": _bool("is_private"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="ipam_role",
        app="ipam",
        endpoint="/api/ipam/roles/",
        display_name="IPAM Role",
        plural="IPAM Roles",
        description="Roles that prefixes, ranges and VLANs are classified by.",
        name_field="name",
        identity_keys=("slug",),
        icon="tag",
        columns=("name", "slug", "weight", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "weight": _int("weight"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="vlan_group",
        app="ipam",
        endpoint="/api/ipam/vlan-groups/",
        display_name="VLAN Group",
        plural="VLAN Groups",
        description="VLAN groups and the VID ranges they own.",
        name_field="name",
        identity_keys=("slug",),
        icon="layers",
        columns=("name", "slug", "scope", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "scope_type": _text("scope_type"),
            "scope": _text("scope.name"),
            "vid_ranges": _list("vid_ranges"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="vrf",
        app="ipam",
        endpoint="/api/ipam/vrfs/",
        display_name="VRF",
        plural="VRFs",
        description="Virtual routing and forwarding instances.",
        name_field="name",
        identity_keys=("name", "rd"),
        icon="route",
        columns=("name", "rd", "tenant", "description"),
        search_fields=("name", "rd", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "rd": _text("rd"),
            "tenant": _text("tenant.slug"),
            "enforce_unique": _bool("enforce_unique"),
            "description": _text("description"),
        },
        relationships=(_rel("tenant", "tenant", "tenant.id"),),
    ),
    NetBoxObjectType(
        id="route_target",
        app="ipam",
        endpoint="/api/ipam/route-targets/",
        display_name="Route Target",
        plural="Route Targets",
        description="BGP route targets referenced by VRFs and L2VPNs.",
        name_field="name",
        identity_keys=("name",),
        icon="route",
        columns=("name", "tenant", "description"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="service",
        app="ipam",
        endpoint="/api/ipam/services/",
        display_name="Service",
        plural="Services",
        description="Services a device or virtual machine exposes, with their ports.",
        name_field="name",
        identity_keys=("parent", "name"),
        icon="globe",
        columns=("name", "protocol", "ports", "parent"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "protocol": _text("protocol.value"),
            "ports": _list("ports"),
            # NetBox 4.3 replaced Service.device and Service.virtual_machine with one
            # generic `parent`, so a service can also hang off an FHRP group. The API key
            # for the type is `parent_object_type`, not `parent_type`. `parent.display` is
            # read rather than `parent.name` because an FHRP group has no name. Both
            # spellings are mapped; the two legacy edges below resolve only pre-4.3.
            "device": _text("device.name"),
            "virtual_machine": _text("virtual_machine.name"),
            "parent": _text("parent.display"),
            "parent_object_type": _text("parent_object_type"),
            "description": _text("description"),
        },
        relationships=(
            _rel("device", ResourceType.DEVICE.value, "device.id"),
            _rel("virtual_machine", "virtual_machine", "virtual_machine.id"),
        ),
    ),
    NetBoxObjectType(
        id="asn",
        app="ipam",
        endpoint="/api/ipam/asns/",
        display_name="ASN",
        plural="ASNs",
        description="Autonomous system numbers.",
        identity_keys=("asn",),
        icon="globe",
        columns=("name", "rir", "tenant", "description"),
        search_fields=("name", "description"),
        attributes={
            "asn": _int("asn", nullable=False),
            "rir": _text("rir.name"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(_rel("rir", "rir", "rir.id"),),
    ),
    NetBoxObjectType(
        id="fhrp_group",
        app="ipam",
        endpoint="/api/ipam/fhrp-groups/",
        display_name="FHRP Group",
        plural="FHRP Groups",
        description="First-hop redundancy groups: VRRP, HSRP, GLBP.",
        identity_keys=("protocol", "group_id"),
        icon="route",
        columns=("name", "protocol", "group_id", "description"),
        search_fields=("name", "description"),
        attributes={
            "protocol": _text("protocol.value"),
            "group_id": _int("group_id"),
            "auth_type": _text("auth_type.value"),
            "description": _text("description"),
        },
    ),
)

_VIRTUALIZATION: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="virtual_machine",
        app="virtualization",
        endpoint="/api/virtualization/virtual-machines/",
        display_name="Virtual Machine",
        plural="Virtual Machines",
        description="Virtual machines, which the platform can drive like any Linux host.",
        name_field="name",
        identity_keys=("cluster", "name"),
        icon="server",
        columns=("name", "status", "cluster", "site", "primary_ip"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "status": _text("status.value"),
            "site": _text("site.slug"),
            "cluster": _text("cluster.name"),
            "role": _text("role.slug"),
            "platform": _text("platform.slug"),
            "tenant": _text("tenant.slug"),
            "primary_ip": _text("primary_ip.address"),
            "vcpus": _number("vcpus"),
            "memory": _int("memory"),
            "disk": _int("disk"),
            "description": _text("description"),
        },
        relationships=(
            _rel("cluster", "cluster", "cluster.id"),
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel("platform", "platform", "platform.id"),
        ),
    ),
    NetBoxObjectType(
        id="cluster",
        app="virtualization",
        endpoint="/api/virtualization/clusters/",
        display_name="Cluster",
        plural="Clusters",
        description="Virtualization clusters that virtual machines run on.",
        name_field="name",
        identity_keys=("name",),
        icon="layers",
        columns=("name", "type", "status", "scope"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "type": _text("type.name"),
            "group": _text("group.name"),
            "status": _text("status.value"),
            # NetBox 4.2 replaced Cluster.site with a generic scope -- a region, a site
            # group, a site or a location -- exactly as it did for Prefix. Both spellings
            # are mapped, and the type is named alongside the value because a bare slug
            # labelled "site" would be a lie for a cluster scoped to a region. The legacy
            # site edge below still resolves pre-4.2 and is simply omitted after; the
            # generic target stays an attribute, since one edge cannot name a target type
            # that varies per row.
            "site": _text("site.slug"),
            "scope": _text("scope.name"),
            "scope_type": _text("scope_type"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(_rel("site", ResourceType.SITE.value, "site.slug"),),
    ),
    NetBoxObjectType(
        id="vm_interface",
        app="virtualization",
        endpoint="/api/virtualization/interfaces/",
        display_name="VM Interface",
        plural="VM Interfaces",
        description="Virtual machine interfaces.",
        name_field="name",
        identity_keys=("virtual_machine", "name"),
        icon="ethernet-port",
        columns=("name", "virtual_machine", "enabled", "mtu"),
        search_fields=("name", "description", "mac_address"),
        attributes={
            "name": _text("name", nullable=False),
            "virtual_machine": _text("virtual_machine.name"),
            "enabled": _bool("enabled"),
            "mtu": _int("mtu"),
            "mac_address": _text("mac_address"),
            "primary_mac_address": _text("primary_mac_address.mac_address"),
            "mode": _text("mode.value"),
            "description": _text("description"),
        },
        relationships=(_rel("virtual_machine", "virtual_machine", "virtual_machine.id"),),
    ),
)

_CIRCUITS: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="circuit",
        app="circuits",
        endpoint="/api/circuits/circuits/",
        display_name="Circuit",
        plural="Circuits",
        description="Carrier circuits: the WAN links between sites and providers.",
        name_field="cid",
        identity_keys=("provider", "cid"),
        icon="cable",
        columns=("name", "provider", "type", "status", "commit_rate"),
        search_fields=("name", "description"),
        attributes={
            "cid": _text("cid", nullable=False),
            "provider": _text("provider.name"),
            "type": _text("type.name"),
            "status": _text("status.value"),
            "tenant": _text("tenant.slug"),
            "install_date": _text("install_date"),
            "termination_date": _text("termination_date"),
            "commit_rate": _int("commit_rate"),
            "description": _text("description"),
        },
        relationships=(
            _rel("circuit_provider", "circuit_provider", "provider.id"),
            _rel("circuit_type", "circuit_type", "type.id"),
        ),
    ),
    NetBoxObjectType(
        id="circuit_type",
        app="circuits",
        endpoint="/api/circuits/circuit-types/",
        display_name="Circuit Type",
        plural="Circuit Types",
        description="Classes of carrier service.",
        name_field="name",
        identity_keys=("slug",),
        icon="tag",
        columns=("name", "slug", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="circuit_provider",
        app="circuits",
        endpoint="/api/circuits/providers/",
        display_name="Circuit Provider",
        plural="Circuit Providers",
        description=(
            "Carriers that supply circuits. Named 'circuit provider' because an "
            "inventory provider is something else entirely here."
        ),
        name_field="name",
        identity_keys=("slug",),
        icon="building",
        columns=("name", "slug", "description"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="provider_network",
        app="circuits",
        endpoint="/api/circuits/provider-networks/",
        display_name="Provider Network",
        plural="Provider Networks",
        description="A carrier's own network, which a circuit can terminate into.",
        name_field="name",
        identity_keys=("provider", "name"),
        icon="globe",
        columns=("name", "provider", "service_id", "description"),
        search_fields=("name", "service_id", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "provider": _text("provider.name"),
            "service_id": _text("service_id"),
            "description": _text("description"),
        },
        relationships=(_rel("circuit_provider", "circuit_provider", "provider.id"),),
    ),
    NetBoxObjectType(
        id="circuit_termination",
        app="circuits",
        endpoint="/api/circuits/circuit-terminations/",
        display_name="Circuit Termination",
        plural="Circuit Terminations",
        description="Where a circuit lands: which side, which site, at what speed.",
        identity_keys=("circuit", "term_side"),
        icon="cable",
        columns=("name", "circuit", "term_side", "termination", "port_speed"),
        search_fields=("description", "xconnect_id"),
        attributes={
            "circuit": _text("circuit.cid"),
            "term_side": _text("term_side"),
            # NetBox 4.2 replaced the site and provider_network foreign keys with one
            # generic `termination`, which may be a region, site group, site, location or
            # provider network. All three spellings are mapped so one catalogue serves
            # either release; the two legacy edges below resolve only pre-4.2.
            "site": _text("site.slug"),
            "provider_network": _text("provider_network.name"),
            "termination": _text("termination.name"),
            "termination_type": _text("termination_type"),
            "port_speed": _int("port_speed"),
            "upstream_speed": _int("upstream_speed"),
            "xconnect_id": _text("xconnect_id"),
            "description": _text("description"),
        },
        relationships=(
            _rel("circuit", "circuit", "circuit.id"),
            # Both pre-4.2 targets get an edge, not just the site: each is a fixed
            # foreign key to something this catalogue registers, so both are traversable
            # where the generic `termination` -- polymorphic, and so attribute-only --
            # cannot be. On 4.2+ neither field is served and both edges are omitted.
            _rel("site", ResourceType.SITE.value, "site.slug"),
            _rel("provider_network", "provider_network", "provider_network.id"),
        ),
    ),
)

_WIRELESS: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="wireless_lan",
        app="wireless",
        endpoint="/api/wireless/wireless-lans/",
        display_name="Wireless LAN",
        plural="Wireless LANs",
        description="SSIDs and how they are authenticated and mapped to VLANs.",
        name_field="ssid",
        identity_keys=("ssid",),
        icon="wifi",
        columns=("name", "status", "vlan", "auth_type"),
        search_fields=("name", "description"),
        attributes={
            "ssid": _text("ssid", nullable=False),
            "group": _text("group.name"),
            "status": _text("status.value"),
            "vlan": _int("vlan.vid"),
            "auth_type": _text("auth_type.value"),
            "auth_cipher": _text("auth_cipher.value"),
            # 4.2 added the generic scope here too; nothing was removed, so this is the
            # only spelling.
            "scope": _text("scope.name"),
            "scope_type": _text("scope_type"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
        relationships=(_rel(ResourceType.VLAN.value, ResourceType.VLAN.value, "vlan.id"),),
    ),
    NetBoxObjectType(
        id="wireless_link",
        app="wireless",
        endpoint="/api/wireless/wireless-links/",
        display_name="Wireless Link",
        plural="Wireless Links",
        description="Point-to-point wireless links between two interfaces.",
        identity_keys=("interface_a", "interface_b"),
        icon="wifi",
        columns=("name", "status", "ssid", "distance"),
        search_fields=("ssid", "description"),
        attributes={
            "interface_a": _text("interface_a.name"),
            "interface_b": _text("interface_b.name"),
            "ssid": _text("ssid"),
            "status": _text("status.value"),
            "distance": _number("distance"),
            "distance_unit": _text("distance_unit.value"),
            "description": _text("description"),
        },
        relationships=(
            _rel("interface_a", "interface", "interface_a.id"),
            _rel("interface_b", "interface", "interface_b.id"),
        ),
    ),
)

_VPN: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="vpn_tunnel",
        app="vpn",
        endpoint="/api/vpn/tunnels/",
        display_name="VPN Tunnel",
        plural="VPN Tunnels",
        description="Site-to-site tunnels and their encapsulation.",
        name_field="name",
        identity_keys=("name",),
        icon="route",
        columns=("name", "status", "encapsulation", "tunnel_id"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "status": _text("status.value"),
            "encapsulation": _text("encapsulation.value"),
            "tunnel_id": _int("tunnel_id"),
            "group": _text("group.name"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="l2vpn",
        app="vpn",
        endpoint="/api/vpn/l2vpns/",
        display_name="L2VPN",
        plural="L2VPNs",
        description="Layer 2 VPNs: VPLS, EVPN, VXLAN and friends.",
        name_field="name",
        identity_keys=("slug",),
        icon="route",
        columns=("name", "slug", "type", "identifier"),
        search_fields=("name", "slug", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "slug": _text("slug"),
            "type": _text("type.value"),
            "identifier": _int("identifier"),
            "tenant": _text("tenant.slug"),
            "description": _text("description"),
        },
    ),
)

_EXTRAS: tuple[NetBoxObjectType, ...] = (
    NetBoxObjectType(
        id="config_context",
        app="extras",
        endpoint="/api/extras/config-contexts/",
        display_name="Config Context",
        plural="Config Contexts",
        description=(
            "NetBox's rendered per-device settings data: the intended values a flow can "
            "template into a command instead of duplicating them into flow inputs."
        ),
        name_field="name",
        identity_keys=("name",),
        icon="file-json",
        columns=("name", "weight", "is_active", "description"),
        search_fields=("name", "description"),
        attributes={
            "name": _text("name", nullable=False),
            "weight": _int("weight"),
            "is_active": _bool("is_active"),
            "data": _json("data"),
            "description": _text("description"),
        },
    ),
    NetBoxObjectType(
        id="journal_entry",
        app="extras",
        endpoint="/api/extras/journal-entries/",
        display_name="Journal Entry",
        plural="Journal Entries",
        description="NetBox's own per-object history: what people recorded, and when.",
        identity_keys=("id",),
        icon="notebook-pen",
        columns=("name", "kind", "created_by", "assigned_object_type"),
        search_fields=("comments",),
        attributes={
            "assigned_object_type": _text("assigned_object_type"),
            "assigned_object_id": _int("assigned_object_id"),
            "created": _text("created"),
            "created_by": _text("created_by"),
            "kind": _text("kind.value"),
            "comments": _text("comments"),
        },
    ),
)

#: Every NetBox family this plugin can read, in catalogue order.
OBJECT_TYPES: tuple[NetBoxObjectType, ...] = (
    *_TENANCY,
    *_DCIM,
    *_IPAM,
    *_VIRTUALIZATION,
    *_CIRCUITS,
    *_WIRELESS,
    *_VPN,
    *_EXTRAS,
)

BY_ID: Mapping[str, NetBoxObjectType] = {entry.id: entry for entry in OBJECT_TYPES}

#: Every catalogue id, sorted, for error messages and documentation.
ALL_OBJECT_TYPE_IDS: tuple[str, ...] = tuple(sorted(BY_ID))


def object_type_specs() -> tuple[ObjectTypeSpec, ...]:
    """Every catalogue family as a platform ``ObjectTypeSpec``, for registration."""
    return tuple(entry.spec for entry in OBJECT_TYPES)


def sources_for(object_type_ids: Iterable[str]) -> dict[str, NetBoxObjectType]:
    """The catalogue entries for ``object_type_ids``, ignoring ids it does not know."""
    return {
        object_type: BY_ID[object_type] for object_type in object_type_ids if object_type in BY_ID
    }
