# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Object-type declaration API.

A plugin declares an object type with an ``ObjectTypeSpec``: an identity, a field
schema (JSON Schema, optionally derived from a Pydantic model), relationship hints, and
UI hints. The core platform exposes these as manifests so the schema-driven UI can render
list/detail/form pages and inventory-submenu entries with no plugin JavaScript.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .identifiers import validate_object_type_id


@dataclass(frozen=True, slots=True)
class ObjectTypeRelationshipSpec:
    """Declares a relationship edge this object type can carry."""

    rel: str
    target_object_type: str
    label: str | None = None
    many: bool = False


@dataclass(frozen=True, slots=True)
class ObjectTypeUIHints:
    """Optional hints driving the generic schema-driven UI.

    ``custom_component`` is the escape hatch: a core-bundled component id for a bespoke
    renderer. It is never plugin-shipped JavaScript.
    """

    icon: str | None = None
    columns: tuple[str, ...] = ()
    detail_layout: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    default_sort: str | None = None
    custom_component: str | None = None

    def __post_init__(self) -> None:
        # Plugin JavaScript is never loaded. Until the core publishes an explicit
        # component allow-list, accepting an arbitrary id advertises a renderer that
        # cannot exist.
        if self.custom_component is not None:
            raise ValueError("custom_component is not supported by this SDK ABI")


@dataclass(frozen=True, slots=True)
class ObjectTypeSpec:
    """A registered inventory object type."""

    id: str
    display_name: str
    plural: str | None = None
    description: str | None = None
    #: Display grouping for a plugin that registers many types, e.g. "IPAM" or "DCIM".
    #: A source system's own families are the natural grouping, and a flat list of
    #: fifty checkboxes is not a choice anyone can make. ``None`` groups with the rest.
    group: str | None = None
    field_schema: Mapping[str, Any] = field(default_factory=dict)
    identity_keys: tuple[str, ...] = ()
    relationships: tuple[ObjectTypeRelationshipSpec, ...] = ()
    ui: ObjectTypeUIHints = field(default_factory=ObjectTypeUIHints)
    #: JSON Schema for the filters accepted when this family is queried.
    #:
    #: This is deliberately separate from ``field_schema``. A response property is
    #: not necessarily filterable, and a source can expose lookup expressions such as
    #: ``last_updated__gte`` that are not response properties at all. Keeping the two
    #: contracts apart lets a platform build a query editor without teaching it a
    #: provider's private query language or offering fields the source will reject.
    #: Appended to preserve positional construction of the older spec fields.
    query_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_object_type_id(self.id)
        for relationship in self.relationships:
            validate_object_type_id(
                relationship.target_object_type,
                field_name=f"relationship {relationship.rel!r} target_object_type",
            )

    @classmethod
    def from_model(
        cls,
        *,
        id: str,
        display_name: str,
        model: Any,
        plural: str | None = None,
        description: str | None = None,
        group: str | None = None,
        query_schema: Mapping[str, Any] | None = None,
        identity_keys: Sequence[str] = (),
        relationships: Sequence[ObjectTypeRelationshipSpec] = (),
        ui: ObjectTypeUIHints | None = None,
    ) -> ObjectTypeSpec:
        """Build a spec whose ``field_schema`` is the Pydantic model's JSON Schema."""
        return cls(
            id=id,
            display_name=display_name,
            plural=plural,
            description=description,
            group=group,
            field_schema=model.model_json_schema(),
            query_schema=query_schema or {},
            identity_keys=tuple(identity_keys),
            relationships=tuple(relationships),
            ui=ui or ObjectTypeUIHints(),
        )
