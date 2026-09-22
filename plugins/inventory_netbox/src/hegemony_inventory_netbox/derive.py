# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Derive the object-type catalogue from a NetBox OpenAPI document.

Every NetBox serves ``/api/schema/``, a complete OpenAPI 3 description of its own REST
API generated from the models it is actually running. That document already contains
almost everything :mod:`.object_types` states by hand -- which families exist, which app
each belongs to, what properties each carries, their types, their nullability, their
enum sets -- and, unlike a hand-written catalogue, it is right about the release in
front of it rather than the release someone had in mind. Plugin models appear in it too,
under ``/api/plugins/<base_url>/``, which no hand-written catalogue can ever anticipate.

This module turns that document into the same :class:`~.object_types.NetBoxObjectType`
entries the catalogue declares, so the two can be compared directly. What the schema
cannot say is declared once in :mod:`.overlay`.

**Object-type ids are app-qualified**: ``dcim.interfaces``, not ``interface``. The id is
the endpoint with its app, which makes it derivable with no table of exceptions and
unique by construction -- NetBox serves both ``/api/dcim/interfaces/`` and
``/api/virtualization/interfaces/``, and a bare singular would have to invent a name for
one of them. See :data:`~.object_types.LEGACY_OBJECT_TYPE_IDS` for the mapping to the
ids this plugin registers today.

Four property shapes appear in a NetBox schema, and each maps to one dotted path:

``{"type": "string", "maxLength": 100}``
    A plain value. The path is the property name.

``{"type": "object", "properties": {"value": ..., "label": ...}}``
    A rendered choice. The path is ``<name>.value``; the enum set is kept.

``{"enum": [...], "type": "string"}``
    *Also* a choice, but rendered flat. The distinction is invisible in the API
    reference and easy to get wrong by hand -- reading ``<name>.value`` from one of
    these yields ``None`` forever, which is how three such fields shipped blank.

``{"allOf": [{"$ref": "#/components/schemas/BriefSite"}], "nullable": true}``
    A reference. The path is ``<name>.<natural key>``, and the reference also becomes a
    relationship edge when its target is a family this deriver produced.

A fifth shape carries no type at all -- ``{"nullable": true, "readOnly": true}`` -- and
is NetBox's generic (polymorphic) reference: ``Prefix.scope`` is a site, a region, a
site group or a location depending on a sibling ``scope_type`` field. Nothing in the
schema says what it holds, so :data:`~.overlay.UNTYPED_PATHS` says instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from hegemony_inventory_sdk import RelationshipMapping

from .catalogue_model import Attr, NetBoxObjectType, qualified_object_type_id
from .overlay import (
    DEFAULT_EXTERNAL_ID_KEY,
    DEFAULT_OVERLAY,
    REGISTERED_IDS_BY_DERIVED,
    Overlay,
    ReplacedField,
)

__all__ = [
    "DerivedCatalogue",
    "DiscoveryReport",
    "SourceStatus",
    "compare_to_catalogue",
    "custom_field_attributes",
    "derive_catalogue",
    "read_status",
    "registered_catalogue",
]

_LIST_ENDPOINT = re.compile(r"^/api/(?P<app>[a-z0-9-]+)/(?P<family>[a-z0-9-]+)/$")
_PLUGIN_ENDPOINT = re.compile(r"^/api/plugins/(?P<app>[a-z0-9_-]+)/(?P<family>[a-z0-9-]+)/$")

#: Properties every NetBox object carries that describe the API rather than the object.
#: ``id`` is already the external id, and the two URLs are self-links.
_API_METADATA: frozenset[str] = frozenset({"id", "url", "display", "display_url"})

#: NetBox denormalises child counts onto parents (``device_count``, ``count_ipaddresses``).
#: They are recomputed server-side on every read and say nothing about the object itself.
_COUNT_SUFFIX = "_count"
_COUNT_PREFIX = "count_"

_SCALAR_TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean"})

# Controls how NetBox renders or pages a response rather than which objects belong in
# it. Offering these in a scope builder would let a saved "filter" truncate a sync.
_QUERY_CONTROL_PARAMETERS: frozenset[str] = frozenset(
    {"brief", "depth", "exclude", "fields", "format", "limit", "offset", "ordering"}
)

#: How NetBox names the two properties that encode a generic reference beside the
#: rendered object itself: ``scope_type``/``scope_id``, ``parent_object_type``/
#: ``parent_object_id``. Both spellings are in use, so both are recognised.
_GENERIC_REF_SUFFIXES: tuple[str, ...] = ("_type", "_id", "_object_type", "_object_id")

#: Suffixes NetBox appends to a serializer's name without changing the model it renders.
#: ``/api/dcim/devices/`` returns ``DeviceWithConfigContext`` rows, while every reference
#: to a device resolves to ``BriefDevice`` -> ``Device``. Indexing a family under its
#: undecorated name too is what lets the two meet; without it a device has attributes but
#: no interfaces pointing at it.
_SERIALIZER_SUFFIXES: tuple[str, ...] = ("WithConfigContext",)

#: NetBox custom-field type -> JSON Schema type. ``object``/``multiobject`` fields hold a
#: rendered reference rather than a scalar, so they keep their whole shape.
_CUSTOM_FIELD_TYPES: Mapping[str, str] = {
    "text": "string",
    "longtext": "string",
    "url": "string",
    "date": "string",
    "datetime": "string",
    "select": "string",
    "integer": "integer",
    "decimal": "number",
    "boolean": "boolean",
    "multiselect": "array",
    "multiobject": "array",
    "json": "object",
    "object": "object",
}


def _titleize(segment: str) -> str:
    return " ".join(word.capitalize() for word in segment.replace("_", "-").split("-"))


#: Endings that are already singular, so a trailing "s" must not be stripped from
#: words such as "chassis" and "status".
_INVARIANT_ENDINGS: tuple[str, ...] = ("ss", "is", "us")


def _singularize(segment: str) -> str:
    """A NetBox endpoint segment as a singular, e.g. ``ip-addresses`` -> ``ip-address``.

    English, not a dictionary: enough for endpoint names, which are machine-generated
    from Django model names and so are regular apart from the invariant plurals.
    """
    for suffix, replacement in (("ies", "y"), ("sses", "ss"), ("xes", "x")):
        if segment.endswith(suffix) and segment != suffix:
            return segment[: -len(suffix)] + replacement
    if segment.endswith("s") and not segment.endswith(_INVARIANT_ENDINGS):
        return segment[:-1]
    return segment


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """What ``/api/status/`` says about the NetBox on the other end.

    Far cheaper than the schema -- a few hundred bytes against several megabytes -- and
    it carries both of the things worth knowing before deciding to fetch the schema at
    all: which release this is, and which plugins are installed. A cached schema is only
    valid for the version that produced it.
    """

    version: str | None = None
    full_version: str | None = None
    plugins: Mapping[str, str] = field(default_factory=dict)

    @property
    def safe_details(self) -> dict[str, Any]:
        """The status as connection-test detail: no credentials, no instance data."""
        details: dict[str, Any] = {}
        if self.version:
            details["netbox_version"] = self.version
        if self.full_version:
            details["netbox_full_version"] = self.full_version
        if self.plugins:
            details["netbox_plugins"] = dict(sorted(self.plugins.items()))
        return details


def read_status(payload: Mapping[str, Any]) -> SourceStatus:
    """Parse an ``/api/status/`` body. Unknown or missing keys yield ``None``."""
    plugins = payload.get("plugins")
    return SourceStatus(
        version=_as_text(payload.get("netbox-version")),
        full_version=_as_text(payload.get("netbox-full-version")),
        plugins={
            str(name): str(version)
            for name, version in sorted(plugins.items())
            if isinstance(name, str)
        }
        if isinstance(plugins, Mapping)
        else {},
    )


def _as_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def custom_field_attributes(
    custom_fields: Iterable[Mapping[str, Any]], *, content_type: str
) -> dict[str, Attr]:
    """Custom fields declared for ``content_type``, as stored attributes.

    NetBox's ``/api/extras/custom-fields/`` enumerates every custom field with the
    content types it applies to, so an instance's own extensions are as discoverable as
    its built-in properties. They arrive nested under ``custom_fields`` on the object
    itself, which is where the dotted path points.

    ``content_type`` is a Django label -- ``dcim.device``, not the endpoint. It is
    rebuilt from the endpoint, corrected by :data:`~.overlay.CONTENT_TYPES` where the
    model is not named after the segment, and :func:`derive_catalogue` records it for
    each family it produces.
    """
    derived: dict[str, Attr] = {}
    for field_definition in custom_fields:
        name = _as_text(field_definition.get("name"))
        if name is None:
            continue
        applies_to = field_definition.get("object_types") or ()
        if isinstance(applies_to, str) or content_type not in applies_to:
            continue
        raw_type = field_definition.get("type")
        type_value = raw_type.get("value") if isinstance(raw_type, Mapping) else raw_type
        json_type = _CUSTOM_FIELD_TYPES.get(str(type_value), "string")
        derived[f"custom_field_{name}"] = Attr(f"custom_fields.{name}", json_type)
    return derived


@dataclass(frozen=True, slots=True)
class DerivedCatalogue:
    """Everything one NetBox schema says this provider could read from it."""

    object_types: tuple[NetBoxObjectType, ...]
    #: Django content-type label per derived id, for matching custom fields.
    content_types: Mapping[str, str]
    source_version: str | None = None
    #: Anything the deriver could not resolve, in the order it was met. A warning is
    #: never fatal -- the attribute is simply not derived -- but each one is either a
    #: NetBox shape this module does not know yet or an overlay entry that has gone
    #: stale, and both are worth seeing.
    warnings: tuple[str, ...] = ()


class _SchemaIndex:
    """Component lookup and natural-key resolution for one OpenAPI document."""

    def __init__(self, schema: Mapping[str, Any], overlay: Overlay) -> None:
        components = schema.get("components")
        self._components: Mapping[str, Any] = (
            components.get("schemas", {}) if isinstance(components, Mapping) else {}
        )
        self._overlay = overlay

    def component(self, name: str) -> Mapping[str, Any]:
        found = self._components.get(name)
        return found if isinstance(found, Mapping) else {}

    def properties(self, name: str) -> Mapping[str, Any]:
        properties = self.component(name).get("properties")
        return properties if isinstance(properties, Mapping) else {}

    def natural_key(self, component: str) -> str:
        """The property of ``component`` that identifies the object it describes.

        NetBox marks its identifying properties required and *writable*: a prefix is
        identified by ``prefix``, an IP by ``address``, a circuit by ``cid``. Everything
        else required on the same object -- the self-links, the display label, the
        denormalised child counts, the MPTT depth -- is ``readOnly``, computed by NetBox
        rather than set by whoever created the object. So the first required, writable
        scalar is nearly always the right answer.

        Where it is not -- a slug the platform keys on, a VLAN's numeric id -- the
        overlay says so. Those are choices between two equally identifying properties,
        which no schema can make.
        """
        override = self._overlay.natural_keys.get(component)
        if override is not None:
            return override
        properties = self.properties(component)
        for candidate in self.component(component).get("required", ()):
            if candidate in _API_METADATA or candidate.startswith("_"):
                continue
            declared = properties.get(candidate, {})
            if declared.get("readOnly"):
                continue
            if isinstance(declared.get("type"), str) and declared["type"] in _SCALAR_TYPES:
                return str(candidate)
        return "name" if "name" in properties else "id"


def _is_noise(name: str) -> bool:
    return (
        name in _API_METADATA
        or name.startswith("_")
        or name.endswith(_COUNT_SUFFIX)
        or name.startswith(_COUNT_PREFIX)
    )


def _referenced_component(prop: Mapping[str, Any]) -> str | None:
    ref = prop.get("$ref")
    if not isinstance(ref, str):
        all_of = prop.get("allOf")
        if isinstance(all_of, Sequence) and len(all_of) == 1 and isinstance(all_of[0], Mapping):
            ref = all_of[0].get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _choice(prop: Mapping[str, Any]) -> tuple[str, tuple[str | int, ...]] | None:
    """``(<value type>, <choices>)`` for a rendered choice object, else ``None``."""
    if prop.get("type") != "object":
        return None
    sub = prop.get("properties")
    if not isinstance(sub, Mapping) or not {"value", "label"} <= set(sub):
        return None
    value = sub["value"] if isinstance(sub["value"], Mapping) else {}
    value_type = value.get("type")
    return (
        str(value_type)
        if isinstance(value_type, str) and value_type in _SCALAR_TYPES
        else "string",
        _enum_of(value),
    )


def _enum_of(prop: Mapping[str, Any]) -> tuple[str | int, ...]:
    """The choice set a property declares, dropping NetBox's empty "unset" member."""
    values = prop.get("enum")
    if not isinstance(values, Sequence) or isinstance(values, str):
        return ()
    return tuple(v for v in values if isinstance(v, str | int) and v != "")


@dataclass(frozen=True, slots=True)
class _Family:
    """One list endpoint and the component its rows use."""

    endpoint: str
    app: str
    segment: str
    component: str

    @property
    def id(self) -> str:
        return qualified_object_type_id(self.endpoint)

    def content_type(self, overlay: Overlay) -> str:
        """The Django label NetBox uses for this model, e.g. ``ipam.ipaddress``.

        Nothing in an OpenAPI document states it -- a NetBox schema contains no content
        type anywhere -- and the component name is the *serializer*, which does not
        always match (``DeviceWithConfigContext`` renders ``dcim.device``). So it is
        built from the endpoint, whose segment is the model name pluralised, and the
        overlay corrects whatever the singularisation rule cannot reach.
        """
        override = overlay.content_types.get(self.endpoint)
        if override is not None:
            return override
        model = _singularize(self.segment).replace("-", "").replace("_", "")
        return f"{self.app.replace('-', '_')}.{model}"


def _list_families(
    schema: Mapping[str, Any], index: _SchemaIndex, overlay: Overlay
) -> list[_Family]:
    """Every collection endpoint the document serves, in document order."""
    paths = schema.get("paths")
    if not isinstance(paths, Mapping):
        return []
    families: list[_Family] = []
    for path, operations in paths.items():
        if not isinstance(path, str) or path in overlay.skip_endpoints:
            continue
        match = _LIST_ENDPOINT.match(path) or _PLUGIN_ENDPOINT.match(path)
        if match is None or not isinstance(operations, Mapping):
            continue
        component = _list_item_component(operations, index)
        if component is None:
            continue
        families.append(
            _Family(
                endpoint=path,
                app=match["app"],
                segment=match["family"],
                component=component,
            )
        )
    return families


def _list_item_component(operations: Mapping[str, Any], index: _SchemaIndex) -> str | None:
    """Resolve ``get -> 200 -> PaginatedXList -> results[] -> X``."""
    get = operations.get("get")
    if not isinstance(get, Mapping):
        return None
    responses = get.get("responses")
    if not isinstance(responses, Mapping):
        return None
    ok = responses.get("200")
    if not isinstance(ok, Mapping):
        return None
    content = ok.get("content")
    if not isinstance(content, Mapping):
        return None
    body = content.get("application/json")
    if not isinstance(body, Mapping):
        return None
    envelope = _referenced_component(body.get("schema") or {})
    if envelope is None:
        return None
    results = index.properties(envelope).get("results")
    if not isinstance(results, Mapping):
        return None
    items = results.get("items")
    return _referenced_component(items) if isinstance(items, Mapping) else None


def derive_catalogue(
    schema: Mapping[str, Any],
    *,
    overlay: Overlay = DEFAULT_OVERLAY,
    custom_fields: Iterable[Mapping[str, Any]] = (),
) -> DerivedCatalogue:
    """Turn one NetBox OpenAPI document into object types.

    ``custom_fields`` is an ``/api/extras/custom-fields/`` listing. Custom fields are not
    in the schema -- NetBox renders them into a single untyped ``custom_fields`` object
    -- so they are derived from their own endpoint and attached to the families they
    apply to.
    """
    index = _SchemaIndex(schema, overlay)
    families = _list_families(schema, index, overlay)
    by_component: dict[str, str] = {}
    for family in families:
        by_component[family.component] = family.id
        by_component.setdefault(_undecorated(family.component), family.id)
    by_endpoint = {family.endpoint: family.id for family in families}
    # The overlay names endpoints; edges name derived ids. Translate once.
    external_id_keys = {
        by_endpoint[endpoint]: key
        for endpoint, key in overlay.external_id_keys.items()
        if endpoint in by_endpoint
    }
    custom_fields = tuple(custom_fields)

    warnings: list[str] = []
    derived: list[NetBoxObjectType] = []
    for family in families:
        derived.append(
            _derive_family(
                family,
                index,
                overlay,
                by_component,
                by_endpoint,
                external_id_keys,
                custom_fields,
                warnings,
                _query_schema(schema, family.endpoint),
            )
        )
    warnings.extend(_stale_overlay_warnings(overlay, {family.endpoint for family in families}))
    info = schema.get("info")
    return DerivedCatalogue(
        object_types=tuple(derived),
        content_types={family.id: family.content_type(overlay) for family in families},
        source_version=_as_text(info.get("version")) if isinstance(info, Mapping) else None,
        warnings=tuple(warnings),
    )


def _derive_family(
    family: _Family,
    index: _SchemaIndex,
    overlay: Overlay,
    by_component: Mapping[str, str],
    by_endpoint: Mapping[str, str],
    external_id_keys: Mapping[str, str],
    custom_fields: Sequence[Mapping[str, Any]],
    warnings: list[str],
    query_schema: Mapping[str, Any],
) -> NetBoxObjectType:
    attributes: dict[str, Attr] = {}
    relationships: list[RelationshipMapping] = []

    for name, prop in index.properties(family.component).items():
        if not isinstance(prop, Mapping):
            continue
        if _is_noise(name) and (family.endpoint, name) not in overlay.keep_properties:
            continue
        attr, edge = _derive_property(
            name, prop, family, index, overlay, by_component, external_id_keys, warnings
        )
        if attr is not None:
            attributes[name] = attr
        if edge is not None:
            relationships.append(edge)

    replaced = overlay.replaced_fields.get(family.endpoint, ())
    generic_refs = {
        name
        for endpoint, name in (*overlay.untyped_paths, *overlay.untyped_objects)
        if endpoint == family.endpoint
    }
    for pair in replaced:
        if pair.legacy_path is None:
            if pair.legacy not in attributes:
                warnings.append(
                    f"{family.endpoint}: {pair.legacy!r} is declared replaced with no path "
                    "to read it by, and this schema does not serve it either"
                )
            continue
        if pair.legacy in attributes:
            warnings.append(
                f"{family.endpoint}: overlay gives {pair.legacy!r} a path to read it by, "
                "but this schema serves it -- the path is no longer needed"
            )
            continue
        attributes[pair.legacy] = Attr(pair.legacy_path, pair.legacy_type)
        # A release that serves the legacy spelling should keep its edge too, or the
        # object arrives with the name of what it points at and no way to reach it.
        target = by_endpoint.get(pair.legacy_target or "")
        if target is not None:
            edge_key = external_id_keys.get(target, DEFAULT_EXTERNAL_ID_KEY)
            relationships.append(
                RelationshipMapping(
                    rel=pair.legacy,
                    target_object_type=target,
                    source=f"{pair.legacy}.{edge_key}",
                )
            )

    for name, path in overlay.extra_attributes.get(family.endpoint, {}).items():
        attributes.setdefault(name, Attr(path))

    attributes.update(
        custom_field_attributes(custom_fields, content_type=family.content_type(overlay))
    )

    if family.endpoint == "/api/ipam/ip-addresses/":
        for name, path, target_endpoint in (
            ("device", "assigned_object.device", "/api/dcim/devices/"),
            (
                "virtual_machine",
                "assigned_object.virtual_machine",
                "/api/virtualization/virtual-machines/",
            ),
        ):
            attributes[name] = Attr(f"{path}.name")
            target = by_endpoint.get(target_endpoint)
            if target:
                relationships.append(RelationshipMapping(name, target, f"{path}.id"))
        for name, endpoint in (
            ("interface", "/api/dcim/interfaces/"),
            ("vm_interface", "/api/virtualization/interfaces/"),
        ):
            target = by_endpoint.get(endpoint)
            if target:
                relationships.append(RelationshipMapping(name, target, f"_hegemony_{name}.id"))

    presentation = overlay.presentation.get(family.endpoint)
    singular = _singularize(family.segment)
    # What this family calls itself. NetBox has no universal name property -- a prefix
    # is identified by ``prefix``, a circuit by ``cid``, an IP by ``address`` -- and the
    # same rule that reads a reference's natural key answers it here too.
    natural = index.natural_key(family.component)
    name_field = natural if natural in attributes else "display"
    # One key for storing a row and for citing it. A family stored under its numeric id
    # while every edge into it cites its slug is two keys for one thing, which is exactly
    # the shape of the defect that started this work.
    external_id_key = external_id_keys.get(family.id, DEFAULT_EXTERNAL_ID_KEY)
    query_properties = dict(query_schema.get("properties") or {})
    # Compatibility with reduced schemas is limited to verified built-in endpoints.
    # A serializer's tenant attribute does not prove that a custom API filters by it.
    tenant_filter_endpoints = {
        "/api/dcim/devices/",
        "/api/dcim/sites/",
        "/api/dcim/racks/",
        "/api/ipam/prefixes/",
        "/api/ipam/ip-addresses/",
        "/api/ipam/vrfs/",
        "/api/ipam/vlans/",
        "/api/circuits/circuits/",
        "/api/virtualization/virtual-machines/",
    }
    if "tenant" in attributes and family.endpoint in tenant_filter_endpoints:
        query_properties.setdefault(
            "tenant",
            {
                "type": "string",
                "description": "NetBox tenant slug.",
            },
        )
    if family.endpoint == "/api/tenancy/tenants/":
        query_properties.setdefault(
            "slug",
            {
                "type": "string",
                "description": "NetBox tenant slug.",
            },
        )
    effective_query_schema: Mapping[str, Any] = (
        {"type": "object", "properties": query_properties} if query_properties else {}
    )
    return NetBoxObjectType(
        id=family.id,
        app=family.app,
        endpoint=family.endpoint,
        display_name=(presentation.display_name if presentation else None) or _titleize(singular),
        plural=(presentation.plural if presentation else None) or _titleize(family.segment),
        description=(presentation.description if presentation else None) or _describe(family),
        name_field=name_field,
        identity=external_id_key,
        identity_keys=(external_id_key,),
        binding=overlay.bindings.get(family.endpoint),
        attributes=attributes,
        columns=(presentation.columns if presentation else ())
        or _default_columns(attributes, natural, replaced, generic_refs),
        search_fields=(presentation.search_fields if presentation else ())
        or _default_search_fields(attributes, natural, replaced),
        icon=presentation.icon if presentation else None,
        relationships=tuple(relationships),
        query_schema=effective_query_schema,
    )


def _query_schema(schema: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
    """JSON Schema for one endpoint's OpenAPI query parameters."""
    paths = schema.get("paths")
    operations = paths.get(endpoint) if isinstance(paths, Mapping) else None
    if not isinstance(operations, Mapping):
        return {}
    get = operations.get("get")
    if not isinstance(get, Mapping):
        return {}
    parameters: list[Any] = []
    for declared in (operations.get("parameters"), get.get("parameters")):
        if isinstance(declared, Sequence) and not isinstance(declared, str):
            parameters.extend(declared)
    components = schema.get("components")
    component_parameters = (
        components.get("parameters")
        if isinstance(components, Mapping) and isinstance(components.get("parameters"), Mapping)
        else {}
    )
    properties: dict[str, Any] = {}
    for raw in parameters:
        if not isinstance(raw, Mapping):
            continue
        parameter = raw
        ref = raw.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/parameters/"):
            resolved = component_parameters.get(ref.rsplit("/", 1)[-1])
            if isinstance(resolved, Mapping):
                parameter = resolved
        if parameter.get("in") != "query":
            continue
        name = parameter.get("name")
        declared_schema = parameter.get("schema")
        if not isinstance(name, str) or not name or not isinstance(declared_schema, Mapping):
            continue
        if name in _QUERY_CONTROL_PARAMETERS:
            continue
        field_schema = dict(declared_schema)
        description = parameter.get("description")
        if isinstance(description, str) and description:
            field_schema.setdefault("description", description)
        properties[name] = field_schema
    return {"type": "object", "properties": properties} if properties else {}


def _property_shape(
    prop: Mapping[str, Any], index: _SchemaIndex, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Resolve scalar aliases and equivalent primitive union branches in OpenAPI 3.1.

    NetBox uses anyOf for blank formatted strings. A union is only collapsed when
    all its non-null alternatives have the same primitive type; polymorphic object
    references retain their original shape for the overlay.
    """
    result = dict(prop)
    ref = prop.get("$ref")
    if isinstance(ref, str) and ref not in seen:
        target = index.component(ref.rsplit("/", 1)[-1])
        if target and not target.get("properties"):
            return _property_shape(
                {**target, **{k: v for k, v in prop.items() if k != "$ref"}}, index, seen | {ref}
            )
    declared = result.get("type")
    if isinstance(declared, list):
        non_null = [item for item in declared if item != "null"]
        if len(non_null) == 1:
            result["type"] = non_null[0]
            result["nullable"] = "null" in declared
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = result.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        shapes = [
            _property_shape(item, index, seen) for item in branches if isinstance(item, Mapping)
        ]
        types = {
            shape.get("type")
            for shape in shapes
            if isinstance(shape.get("type"), str) and shape.get("type") != "null"
        }
        if (
            len(types) == 1
            and types <= _SCALAR_TYPES
            and all(
                isinstance(shape.get("type"), str) and shape["type"] in types | {"null"}
                for shape in shapes
            )
        ):
            result["type"] = next(iter(types))
            result["nullable"] = bool(result.get("nullable")) or any(
                shape.get("nullable") or shape.get("type") == "null" for shape in shapes
            )
            result.pop(keyword, None)
            # Branch-specific constraints are not constraints on the whole union.
            if all("enum" in shape for shape in shapes):
                result["enum"] = list(
                    dict.fromkeys(v for shape in shapes for v in shape["enum"] if v is not None)
                )
    if not result.get("type") and result.get("enum"):
        values = [v for v in result["enum"] if v is not None]
        if values and all(isinstance(v, str) for v in values):
            result["type"] = "string"
            result["nullable"] = None in result["enum"]
    return result


def _derive_property(
    name: str,
    prop: Mapping[str, Any],
    family: _Family,
    index: _SchemaIndex,
    overlay: Overlay,
    by_component: Mapping[str, str],
    external_id_keys: Mapping[str, str],
    warnings: list[str],
) -> tuple[Attr | None, RelationshipMapping | None]:
    prop = _property_shape(prop, index)
    nullable = bool(prop.get("nullable"))

    component = _referenced_component(prop)
    if component is not None:
        key = index.natural_key(component)
        key_type = index.properties(component).get(key, {}).get("type")
        path = f"{name}.{key}"
        json_type = (
            str(key_type) if isinstance(key_type, str) and key_type in _SCALAR_TYPES else "string"
        )
        target = by_component.get(_dereferenced_family(component))
        if target is None:
            return Attr(path, json_type, nullable), None
        # The attribute stores what a person reads; the edge has to cite the target's
        # external id, which is the key the platform stored it under. They are usually
        # different properties of the same reference.
        edge_key = external_id_keys.get(target, DEFAULT_EXTERNAL_ID_KEY)
        edge = RelationshipMapping(rel=name, target_object_type=target, source=f"{name}.{edge_key}")
        return Attr(path, json_type, nullable), edge

    choice = _choice(prop)
    if choice is not None:
        value_type, values = choice
        return Attr(f"{name}.value", value_type, nullable, values), None

    declared = prop.get("type")
    if isinstance(declared, str) and declared in _SCALAR_TYPES:
        # A bare enum: the same choice set, rendered flat rather than as an object.
        return Attr(name, str(declared), nullable, _enum_of(prop)), None
    if declared == "array":
        return Attr(name, "array", nullable), None
    if declared == "object":
        return Attr(name, "object", nullable), None

    # No type at all: a generic reference, or free-form JSON. Only the overlay knows.
    if (family.endpoint, name) in overlay.untyped_objects or (
        family.endpoint == "/api/extras/config-context-profiles/" and name == "schema"
    ):
        return Attr(name, "object", True), None
    path = overlay.untyped_paths.get((family.endpoint, name))
    if path is None:
        warnings.append(
            f"{family.endpoint}: {name!r} is a generic reference with no type in the "
            "schema and no overlay entry; stored as an opaque object"
        )
        return Attr(name, "object", True), None
    return Attr(path, "string", True), None


def _dereferenced_family(component: str) -> str:
    """``BriefSite`` and ``NestedSite`` both describe the same family as ``Site``."""
    for prefix in ("Brief", "Nested"):
        if component.startswith(prefix):
            return component[len(prefix) :]
    return component


def _undecorated(component: str) -> str:
    """``DeviceWithConfigContext`` names the same model as ``Device``."""
    for suffix in _SERIALIZER_SUFFIXES:
        if component.endswith(suffix) and component != suffix:
            return component[: -len(suffix)]
    return component


def _describe(family: _Family) -> str:
    """A description for this family.

    Not NetBox's: the ``description`` on a component is the docstring of the *serializer*
    that renders it ("Adds support for custom fields and tags."), which describes the
    implementation rather than the objects. Overlay presentation is where a better one
    goes.
    """
    return f"NetBox {_titleize(family.segment).lower()}, as served by {family.endpoint}."


#: Columns worth leading with wherever a family has them. Everything after these comes
#: from NetBox's own field order, so this list is short on purpose: it exists because
#: lifecycle and role are what an operator scans a table for, and NetBox does not always
#: declare them early.
_LEADING_COLUMNS: tuple[str, ...] = ("status", "role")

#: How many columns a derived browse table gets before ``description`` closes it. Six is
#: what fits an inventory table without scrolling; past that a column is not read.
_COLUMN_LIMIT = 6

#: Properties that are never a column: NetBox's own bookkeeping, and the free-form blobs
#: that have no useful cell rendering. Dropped by name rather than by type because
#: ``created`` and ``last_updated`` are perfectly good strings and still not worth a
#: column on every table in the product.
_NEVER_COLUMNS: frozenset[str] = frozenset(
    {
        "comments",
        "config_context",
        "created",
        "custom_field_data",
        "custom_fields",
        "last_updated",
        "local_context_data",
        "notes",
        "tags",
    }
)
#: Properties worth searching. Free text an operator would actually type.
_PREFERRED_SEARCH: tuple[str, ...] = (
    "name",
    "slug",
    "serial",
    "asset_tag",
    "label",
    "description",
)


def _default_columns(
    attributes: Mapping[str, Attr],
    natural: str,
    replaced: Sequence[ReplacedField],
    generic_refs: frozenset[str] | set[str] = frozenset(),
) -> tuple[str, ...]:
    """Default browse columns, taken from the order NetBox itself declares fields in.

    A serializer's field order is an editorial choice NetBox already made: the fields
    that identify and distinguish an object come first, and the bookkeeping comes last.
    Reading columns off that order recovers the great majority of what a person picking
    columns by hand arrives at, and -- unlike a fixed list of favoured names -- it says
    something useful about a family nobody wrote down in advance, which is the whole
    point of deriving a catalogue at all.

    Four corrections are applied to that order:

    * the name field leads, then :data:`_LEADING_COLUMNS`, because lifecycle and role
      are what an operator scans a table for and NetBox does not always declare them
      early;
    * a **legacy** spelling is never a column. A column costs screen space whether or
      not it holds anything, so naming one a current NetBox has replaced leaves every
      operator a permanently blank column -- the defect fixed for ``ip_prefix`` by hand,
      made impossible here. Search fields are free of that cost and deliberately keep
      both spellings; see the plugin README;
    * where the replaced field *was* a column, its replacement takes the place.
      Excluding one without promoting the other leaves the table showing neither, which
      is the same blank column by another route. Which pairs those are is a judgement
      the pair carries (:attr:`~.overlay.ReplacedField.column`), because an interface's
      MAC is worth searching and not worth a column;
    * the two properties that *encode* a generic reference -- ``scope_type`` and
      ``scope_id`` beside ``scope``, ``parent_object_id`` beside ``parent`` -- are
      dropped in favour of the reference itself. They are how the API transports the
      relation, and a table of content-type numbers tells a reader nothing.

    ``description`` closes the table when the family has one, whether or not the limit
    was reached, because a row with no other distinguishing column still has that.
    """
    legacy = {pair.legacy for pair in replaced}
    encoded = {f"{ref}{suffix}" for ref in generic_refs for suffix in _GENERIC_REF_SUFFIXES}
    skip = legacy | encoded | _NEVER_COLUMNS | {"description"}

    chosen = [natural] if natural in attributes and natural not in skip else []
    chosen += [name for name in _LEADING_COLUMNS if name in attributes and name not in skip]
    for name, attr in attributes.items():
        if len(chosen) >= _COLUMN_LIMIT:
            break
        if name in chosen or name in skip or attr.json_type in {"array", "object"}:
            continue
        chosen.append(name)
    chosen += [
        pair.generic
        for pair in replaced
        if pair.column and pair.generic in attributes and pair.generic not in chosen
    ]
    if "description" in attributes and "description" not in chosen:
        chosen.append("description")
    return tuple(chosen) or ("name",)


def _default_search_fields(
    attributes: Mapping[str, Attr], natural: str, replaced: Sequence[ReplacedField]
) -> tuple[str, ...]:
    """Default search fields, carrying both spellings of anything NetBox replaced.

    The mirror of :func:`_default_columns`. A search field costs no screen space and an
    absent attribute simply never matches, so naming both spellings keeps a value
    findable on either side of a rename while naming one leaves it unfindable on the
    other -- which is how an interface stopped being searchable by MAC.
    """
    chosen = [natural] if natural in attributes else []
    chosen += [name for name in _PREFERRED_SEARCH if name in attributes and name != natural]
    for pair in replaced:
        for spelling in (pair.legacy, pair.generic):
            if spelling in attributes and spelling not in chosen:
                chosen.append(spelling)
    return tuple(chosen) or ("name",)


def _stale_overlay_warnings(overlay: Overlay, endpoints: frozenset[str] | set[str]) -> list[str]:
    """Overlay entries naming an endpoint this document does not serve."""
    warnings: list[str] = []
    for endpoint in overlay.replaced_fields:
        if endpoint not in endpoints:
            warnings.append(f"overlay declares replaced fields for unknown endpoint {endpoint}")
    for endpoint, _name in (*overlay.untyped_paths, *overlay.untyped_objects):
        if endpoint not in endpoints:
            warnings.append(f"overlay declares a generic-reference path for unknown {endpoint}")
    for endpoint in overlay.external_id_keys:
        if endpoint not in endpoints:
            warnings.append(f"overlay declares an external id key for unknown endpoint {endpoint}")
    for endpoint in overlay.presentation:
        if endpoint not in endpoints:
            warnings.append(f"overlay declares presentation for unknown endpoint {endpoint}")
    for endpoint, name in overlay.keep_properties:
        if endpoint not in endpoints:
            warnings.append(f"overlay keeps {name!r} on unknown endpoint {endpoint}")
    return warnings


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """What one NetBox instance serves, measured against what this plugin offers.

    The shipped catalogue is a claim about NetBox in general. This is the answer for
    the NetBox in front of you, and the two differ in both directions: a family the
    catalogue offers may not exist on an older release or a trimmed install, and an
    instance serves families -- its plugins' models above all -- that no shipped
    catalogue could have anticipated.
    """

    source_version: str | None
    #: Collection endpoints this instance serves, after the overlay's skip list.
    families_served: int
    #: Catalogue families this instance serves, so they can actually sync.
    families_offered: int
    #: Catalogue families this instance does **not** serve. Ticking one of these on the
    #: provider form is how an object type comes to sync nothing at all, and until now
    #: nothing said so before the first empty sync.
    families_missing: tuple[str, ...]
    #: Families this instance serves that the catalogue does not offer.
    families_undeclared: int
    #: Custom fields defined on this instance. No shipped catalogue can carry these.
    custom_fields: int
    #: Shapes the deriver did not recognise. Empty on a stock 4.3, so anything here is
    #: a later release or a plugin model worth teaching the overlay about.
    warnings: tuple[str, ...]

    @property
    def safe_details(self) -> dict[str, Any]:
        """The report as connection-test detail: counts and type ids, no instance data."""
        details: dict[str, Any] = {
            "netbox_families_served": self.families_served,
            "netbox_families_offered": self.families_offered,
            "netbox_families_undeclared": self.families_undeclared,
            "netbox_custom_fields": self.custom_fields,
        }
        if self.families_missing:
            details["netbox_families_missing"] = list(self.families_missing)
        if self.warnings:
            details["netbox_schema_warnings"] = list(self.warnings)
        return details


def compare_to_catalogue(derived: DerivedCatalogue, *, custom_fields: int = 0) -> DiscoveryReport:
    """Measure a derived catalogue against the one this plugin ships.

    The shipped catalogue is imported here rather than at module scope on purpose: this
    module *generates* that catalogue, and a module-level import would make the
    generator depend on its own previous output.
    """
    from .object_types import OBJECT_TYPES

    served = {entry.endpoint for entry in derived.object_types}
    missing = tuple(entry.id for entry in OBJECT_TYPES if entry.endpoint not in served)
    offered = len(OBJECT_TYPES) - len(missing)
    catalogued = {entry.endpoint for entry in OBJECT_TYPES}
    return DiscoveryReport(
        source_version=derived.source_version,
        families_served=len(served),
        families_offered=offered,
        families_missing=missing,
        families_undeclared=len(served - catalogued),
        custom_fields=custom_fields,
        warnings=derived.warnings,
    )


def registered_catalogue(derived: DerivedCatalogue) -> tuple[NetBoxObjectType, ...]:
    """Re-key a derived catalogue onto the ids this plugin registers.

    Discovery has to **extend** what a provider offers, not shadow it. The deriver names
    families by their endpoint (``dcim.interfaces``), while the plugin registers
    hand-chosen ids (``interface``); returning the derived spelling would leave an
    instance offering both for the same family, and every row already synced under the
    registered id stranded.

    So a family the catalogue already knows keeps its registered id, and only families it
    does not -- a later release's, a NetBox plugin's -- arrive under the derived one.
    Relationship targets are re-keyed the same way, or an edge would point at an id
    nothing serves. :data:`~.overlay.REGISTERED_IDS` is the table both use.

    Devices and sites are in here like anything else. They used to be left out, because
    the platform read them through its own contract and offering them again would have
    stored every device twice under two different keys. Projection is what changes that:
    they are read once, as families, and the devices and sites tables are written from
    the stored rows. :data:`~.overlay.BINDINGS` is what says which is which.
    """
    renamed = REGISTERED_IDS_BY_DERIVED
    catalogue: list[NetBoxObjectType] = []
    for entry in derived.object_types:
        relationships = tuple(
            RelationshipMapping(
                rel=edge.rel,
                target_object_type=renamed.get(edge.target_object_type, edge.target_object_type),
                source=edge.source,
            )
            for edge in entry.relationships
        )
        catalogue.append(
            replace(entry, id=renamed.get(entry.id, entry.id), relationships=relationships)
        )
    return tuple(catalogue)
