# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Declarative record → ``ObjectDescriptor`` mapping.

A provider author adding a new object type usually fetches a list of source records
(dicts from a REST/GraphQL/YAML source) and needs to turn each one into an
:class:`ObjectDescriptor`. Rather than hand-write that extraction, declare an
:class:`ObjectFieldMapping` and call :func:`map_records`. Dotted paths traverse nested
mappings, e.g. ``"platform.slug"`` reads ``record["platform"]["slug"]``.

This helper is the recommended way to implement ``list_objects`` for a custom type;
see the provider plugin READMEs for a worked example.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .descriptors import ObjectDescriptor, ObjectRelationship
from .identifiers import validate_object_type_id


@dataclass(frozen=True, slots=True)
class RelationshipMapping:
    """How to derive one :class:`ObjectRelationship` from a source record."""

    rel: str
    target_object_type: str
    source: str  # dotted path to the related object's external id

    def __post_init__(self) -> None:
        validate_object_type_id(
            self.target_object_type,
            field_name=f"relationship {self.rel!r} target_object_type",
        )


@dataclass(frozen=True, slots=True)
class ObjectFieldMapping:
    """Declarative mapping from a source record to an :class:`ObjectDescriptor`.

    ``identity`` must resolve to a stable, unique external id (materialization
    fail-closes on a missing or duplicate external id).
    """

    object_type: str
    identity: str
    name_field: str
    field_map: Mapping[str, str] = field(default_factory=dict)
    display_name_field: str | None = None
    relationships: tuple[RelationshipMapping, ...] = ()
    #: How the provider reaches this family, in whatever terms the provider uses.
    #:
    #: Opaque to the platform, which stores it and hands it straight back. A mapping
    #: says how to read a record; it does not say where the records come from, and for a
    #: family the provider *discovered* there is nowhere else for that to live -- the
    #: family is absent from the catalogue the provider shipped, and the provider is
    #: built fresh per operation and remembers nothing. So the provider writes down what
    #: it will need (a REST path, a GraphQL type name) and reads it back on the next
    #: call. Values are strings so the whole mapping round-trips through JSON unchanged.
    source: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_object_type_id(self.object_type)


def extract_path(record: Mapping[str, Any], path: str) -> Any:
    """Read a dotted ``path`` from a nested mapping; ``None`` if any segment is missing."""
    current: Any = record
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def map_record(
    record: Mapping[str, Any], mapping: ObjectFieldMapping, *, provider_id: str
) -> ObjectDescriptor:
    """Map one source ``record`` into an :class:`ObjectDescriptor` using ``mapping``."""
    external_id = _coerce_str(extract_path(record, mapping.identity))
    name = _coerce_str(extract_path(record, mapping.name_field)) or external_id or ""
    display_name = (
        _coerce_str(extract_path(record, mapping.display_name_field))
        if mapping.display_name_field
        else None
    )
    attributes = {attr: extract_path(record, path) for attr, path in mapping.field_map.items()}
    relationships = tuple(
        ObjectRelationship(
            rel=relationship.rel,
            target_object_type=relationship.target_object_type,
            target_external_id=target,
        )
        for relationship in mapping.relationships
        if (target := _coerce_str(extract_path(record, relationship.source))) is not None
    )
    return ObjectDescriptor(
        provider_id=provider_id,
        object_type=mapping.object_type,
        external_id=external_id,
        name=name,
        display_name=display_name,
        attributes=attributes,
        relationships=relationships,
    )


def map_records(
    records: Iterable[Mapping[str, Any]], mapping: ObjectFieldMapping, *, provider_id: str
) -> list[ObjectDescriptor]:
    """Map an iterable of source records into :class:`ObjectDescriptor` objects."""
    return [map_record(record, mapping, provider_id=provider_id) for record in records]
