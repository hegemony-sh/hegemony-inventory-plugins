# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Declarative generic inventory definitions; no source-specific object catalogue."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hegemony_inventory_sdk import ObjectFieldMapping, ObjectTypeSpec
from hegemony_inventory_sdk.identifiers import validate_object_type_id
from hegemony_inventory_sdk.objecttype import ObjectTypeRelationshipSpec, ObjectTypeUIHints

# A deliberately bounded subset. In particular, references, regexes and executable
# extensions are not accepted, including in nested schemas.
_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "title",
        "description",
    }
)


def json_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = [100000] if budget is None else budget
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise ValueError("Document nesting or expanded value limit exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            json_value(item, depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for item in value:
            json_value(item, depth=depth + 1, budget=budget)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("Values must be JSON-compatible; quote YAML dates")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite numbers are not supported")


def validate_schema(schema: Any, path: str = "field_schema") -> None:
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ValueError(f"{path}: unsupported schema keyword or schema shape")
    if "type" not in schema:
        raise ValueError(f"{path}.type: required")
    for key, child in schema.get("properties", {}).items():
        if not key or "." in key:
            raise ValueError(
                f"{path}.properties: property names must be nonempty and contain no dots"
            )
        validate_schema(child, f"{path}.properties.{key}")
    if "items" in schema:
        validate_schema(schema["items"], f"{path}.items")
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        validate_schema(additional, f"{path}.additionalProperties")


def field_at(schema: dict[str, Any], path: str) -> dict[str, Any]:
    if "." in path:
        raise ValueError("Metadata and filters currently require top-level attributes")
    current = schema
    for segment in path.split("."):
        current = current.get("properties", {}).get(segment, {})
        if not current:
            raise ValueError(f"Metadata names undeclared attribute: {path}")
    return current


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class RelationshipDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rel: str = Field(min_length=1)
    target_object_type: str
    label: str | None = None
    many: bool = False
    required: bool = False


class TypeDefinition(Envelope):
    kind: Literal["object_type"]
    id: str
    display_name: str = Field(min_length=1)
    plural: str | None = None
    description: str | None = None
    group: str | None = None
    field_schema: dict[str, Any]
    relationships: list[RelationshipDefinition] = Field(default_factory=list)
    ui: dict[str, Any] = Field(default_factory=dict)
    filters: list[str] = Field(default_factory=list)

    def spec(self) -> ObjectTypeSpec:
        validate_object_type_id(self.id)
        if self.id in {"device", "site"}:
            raise ValueError("id: device and site are reserved for legacy inventory")
        Draft202012Validator.check_schema(self.field_schema)
        validate_schema(self.field_schema)
        if self.field_schema.get("type") != "object":
            raise ValueError("field_schema.type must be object")
        if len({r.rel for r in self.relationships}) != len(self.relationships):
            raise ValueError("relationships: duplicate relation name")
        allowed_ui = {"icon", "columns", "detail_layout", "search_fields", "default_sort"}
        if set(self.ui) - allowed_ui:
            raise ValueError("ui: unsupported metadata")
        ui = dict(self.ui)
        for key in ("columns", "detail_layout", "search_fields"):
            values = ui.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"ui.{key}: expected a list of attribute paths")
            for value in values:
                if value not in {"name", "display_name", "external_id"}:
                    field_at(self.field_schema, value)
            ui[key] = tuple(values)
        sort = ui.get("default_sort")
        if sort is not None:
            if not isinstance(sort, str):
                raise ValueError("ui.default_sort: expected a string")
            if sort.lstrip("-") not in {"name", "display_name", "external_id"}:
                sort_schema = field_at(self.field_schema, sort.lstrip("-"))
                types = sort_schema["type"]
                if any(
                    t in {"object", "array"} for t in ([types] if isinstance(types, str) else types)
                ):
                    raise ValueError("ui.default_sort: requires a scalar attribute")
        if ui.get("icon") is not None and not isinstance(ui["icon"], str):
            raise ValueError("ui.icon: expected a string")
        query = {
            "q": {"type": "string"},
            "name": {"type": "string"},
            "external_id": {"type": "string"},
        }
        for name in self.filters:
            attribute = field_at(self.field_schema, name)
            kinds = attribute["type"]
            if any(
                k in {"array", "object"} for k in ([kinds] if isinstance(kinds, str) else kinds)
            ):
                raise ValueError(f"filters.{name}: equality filters require scalar attributes")
            query[f"attr.{name}"] = attribute
        return ObjectTypeSpec(
            id=self.id,
            display_name=self.display_name,
            plural=self.plural,
            description=self.description,
            group=self.group,
            field_schema=self.field_schema,
            identity_keys=("external_id",),
            relationships=tuple(
                ObjectTypeRelationshipSpec(
                    rel=r.rel, target_object_type=r.target_object_type, label=r.label, many=r.many
                )
                for r in self.relationships
            ),
            ui=ObjectTypeUIHints(**ui),
            query_schema={"type": "object", "properties": query, "additionalProperties": False},
        )

    def mapping(self) -> ObjectFieldMapping:
        # Mapping metadata describes the envelope. Descriptor construction retains
        # absent attributes and expands many-valued relationships without coercion.
        return ObjectFieldMapping(
            object_type=self.id,
            identity="external_id",
            name_field="name",
            display_name_field="display_name",
            field_map={
                name: f"attributes.{name}" for name in self.field_schema.get("properties", {})
            },
            source={"format": "git-yaml-v1"},
        )


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rel: str
    target_object_type: str
    target_external_id: str = Field(min_length=1)


class Record(Envelope):
    kind: Literal["object"]
    object_type: str
    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    display_name: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)

    @field_validator("external_id", "name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("value must be nonblank with no surrounding whitespace")
        return value


@dataclass
class GenericInventory:
    definitions: dict[str, TypeDefinition] = field(default_factory=dict)
    specs: dict[str, ObjectTypeSpec] = field(default_factory=dict)
    records: dict[str, dict[str, Record]] = field(default_factory=dict)
    files: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        payload = {key: value.model_dump() for key, value in sorted(self.definitions.items())}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_generic(
    base: Path, *, root: Path, remaining_files: int, max_file_bytes: int
) -> GenericInventory:
    # Import locally to share the legacy loader's safety and diagnostic policy.
    from .loader import _load_yaml, _validate_file, _validation_message

    result = GenericInventory()
    files: list[Path] = []
    scanned = 0
    for section in (base / "object-types", base / "objects"):
        if not section.exists() and not section.is_symlink():
            continue
        stack = [section]
        while stack:
            path = stack.pop()
            scanned += 1
            if scanned > max(remaining_files * 4, 100) or len(path.relative_to(base).parts) > 32:
                raise ValueError("Inventory directory traversal limit exceeded")
            if path.is_symlink():
                raise ValueError(f"{path.relative_to(root)}: symlinks are not allowed")
            if path.is_dir():
                stack.extend(sorted(path.iterdir(), reverse=True))
            elif path.suffix.lower() in {".yaml", ".yml"}:
                files.append(path)
                if len(files) > remaining_files:
                    raise ValueError("Inventory file count limit exceeded")
            elif path == section:
                raise ValueError(f"{path.relative_to(root)}: expected directory")
    for path in sorted(files, key=lambda p: (p.parts[len(base.parts)] != "object-types", str(p))):
        rel = str(path.relative_to(root))
        try:
            _validate_file(path, max_file_bytes=max_file_bytes)
            payload = _load_yaml(path)
            json_value(payload)
            if path.is_relative_to(base / "object-types"):
                if path.parent != base / "object-types":
                    raise ValueError("type definitions must be directly inside object-types")
                definition = TypeDefinition.model_validate(payload)
                spec = definition.spec()
                if definition.id in result.definitions:
                    raise ValueError("id: duplicate type definition")
                result.definitions[definition.id] = definition
                result.specs[definition.id] = spec
                result.records[definition.id] = {}
            else:
                record = Record.model_validate(payload)
                if record.object_type not in result.definitions:
                    raise ValueError("object_type: unknown type")
                if path.parent != base / "objects" / record.object_type:
                    raise ValueError("object_type: record directory does not match type")
                definition = result.definitions[record.object_type]
                error = next(
                    Draft202012Validator(definition.field_schema).iter_errors(record.attributes),
                    None,
                )
                if error:
                    location = ".".join(str(p) for p in error.absolute_path)
                    raise ValueError(f"attributes.{location}: {error.validator} validation failed")
                if record.external_id in result.records[record.object_type]:
                    raise ValueError("external_id: duplicate ID within type")
                result.records[record.object_type][record.external_id] = record
                result.files[(record.object_type, record.external_id)] = rel
        except ValidationError as exc:
            raise ValueError(f"{rel}: {_validation_message(exc)}") from None
        except SchemaError:
            raise ValueError(f"{rel}: invalid field_schema") from None
        except (yaml.YAMLError, UnicodeError):
            # Parser exceptions include source snippets; never expose record values.
            raise ValueError(f"{rel}: invalid YAML or text encoding") from None
        except (TypeError, OSError, RecursionError):
            raise ValueError(f"{rel}: invalid document") from None
        except ValueError as exc:
            raise ValueError(f"{rel}: {exc}") from None
    for kind, definition in result.definitions.items():
        directory = base / "objects" / kind
        # Git cannot track an empty directory: an empty type uses a .gitkeep file.
        if not directory.is_dir():
            raise ValueError(
                f"objects/{kind}: missing object directory (use .gitkeep for an empty type)"
            )
        relations = {r.rel: r for r in definition.relationships}
        for relation in relations.values():
            if relation.target_object_type not in result.definitions:
                raise ValueError(f"object-types/{kind}: relationship target type is not declared")
        for record in result.records[kind].values():
            counts: dict[str, int] = {}
            seen: set[tuple[str, str, str]] = set()
            for edge in record.relationships:
                declaration = relations.get(edge.rel)
                identity = (edge.rel, edge.target_object_type, edge.target_external_id)
                if (
                    declaration is None
                    or edge.target_object_type != declaration.target_object_type
                    or edge.target_external_id
                    not in result.records.get(edge.target_object_type, {})
                    or identity in seen
                ):
                    raise ValueError(
                        f"{result.files[(kind, record.external_id)]}: relationships: invalid or missing target"
                    )
                seen.add(identity)
                counts[edge.rel] = counts.get(edge.rel, 0) + 1
            for rel, declaration in relations.items():
                if (not declaration.many and counts.get(rel, 0) > 1) or (
                    declaration.required and not counts.get(rel)
                ):
                    raise ValueError(
                        f"{result.files[(kind, record.external_id)]}: relationships.{rel}: cardinality violation"
                    )
    return result
