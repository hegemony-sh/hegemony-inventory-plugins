# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What one NetBox family is, independent of which families there are.

The catalogue itself is generated (:mod:`.catalogue_generated`) and the corrections
applied to it are hand-written (:mod:`.overlay`). Both need the same vocabulary --
an attribute, a family, the app it belongs to -- so it lives here, where neither
depends on the other.

Two things have to agree for an object type to work end to end: the provider needs an
API endpoint and a record -> descriptor mapping to read it with, and the platform needs
an ``ObjectTypeSpec`` to list, search and render it with. Both are derived from a
:class:`NetBoxObjectType`, so they cannot drift apart.

Field paths are dotted (``status.value`` reads NetBox's rendered choice object). A path
a NetBox release does not serve yields ``None`` rather than an error, which is how one
catalogue serves several releases: where 4.2 renamed something, both spellings are
mapped and the absent one is simply null.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hegemony_inventory_sdk import (
    ObjectFieldMapping,
    ObjectTypeRelationshipSpec,
    ObjectTypeSpec,
    ObjectTypeUIHints,
    ProjectionBinding,
    RelationshipMapping,
)


@dataclass(frozen=True, slots=True)
class Attr:
    """One stored attribute: where to read it, and what shape it has."""

    path: str
    json_type: str = "string"
    nullable: bool = True
    #: The values this attribute may take, when the source constrains them. A NetBox
    #: schema states the choice set for every status, type and role field; a
    #: hand-written entry almost never does, because keeping thirty such lists correct
    #: by hand is not work anyone would choose. :mod:`.derive` fills this in, so a
    #: derived catalogue can offer an exact filter where a written one can only offer a
    #: text box.
    enum: tuple[str | int, ...] = ()

    @property
    def schema(self) -> dict[str, Any]:
        """This attribute as a JSON Schema property."""
        schema: dict[str, Any] = {
            "type": [self.json_type, "null"] if self.nullable else self.json_type
        }
        if self.enum:
            # A nullable enum has to admit null explicitly: JSON Schema intersects
            # `type` and `enum`, so omitting it would exclude the null the type allows.
            schema["enum"] = [*self.enum, None] if self.nullable else list(self.enum)
        return schema


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


def qualified_object_type_id(endpoint: str) -> str:
    """The app-qualified id for a NetBox list endpoint, e.g. ``dcim.ip_addresses``.

    An object-type id has to be unique across everything a provider registers, and the
    endpoint already is: NetBox serves both ``/api/dcim/interfaces/`` and
    ``/api/virtualization/interfaces/``, so a bare singular has to invent a name for one
    of them and keep a table of the inventions. Qualifying by app needs no table and no
    invention, which is what lets :mod:`.derive` produce ids for families -- plugin
    models included -- that nobody wrote down in advance.

    Plugin endpoints live under ``/api/plugins/<app>/<family>/`` and qualify by their
    own app, so two NetBox plugins cannot collide either.
    """
    segments = endpoint.strip("/").split("/")
    if segments[:2] == ["api", "plugins"]:
        app, family = segments[2], segments[3]
    else:
        app, family = segments[1], segments[2]
    return f"{app.replace('-', '_')}.{family.replace('-', '_')}"


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
    #: JSON Schema for filters accepted by this family's collection endpoint.
    query_schema: Mapping[str, Any] = field(default_factory=dict)
    #: Extra NetBox filter always applied to this family, on top of any configured one.
    base_query: str = ""
    #: How this family becomes a platform device or site, when it is one. ``None`` for
    #: every family that is only ever a generic object, which is almost all of them.
    binding: ProjectionBinding | None = None

    @property
    def qualified_id(self) -> str:
        """This family's app-qualified id; see :func:`qualified_object_type_id`."""
        return qualified_object_type_id(self.endpoint)

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
            # Where to fetch it. Opaque to the platform, which stores it with the rest of
            # the mapping and hands it back when it asks for a family this provider
            # discovered -- one that is, by definition, absent from the catalogue below.
            source={"endpoint": self.endpoint, "base_query": self.base_query},
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
            query_schema=self.query_schema,
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
