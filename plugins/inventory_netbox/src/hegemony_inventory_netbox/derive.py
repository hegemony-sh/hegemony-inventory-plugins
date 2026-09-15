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
from dataclasses import dataclass, field
from typing import Any

from hegemony_inventory_sdk import RelationshipMapping

from .object_types import Attr, NetBoxObjectType, qualified_object_type_id
from .overlay import DEFAULT_OVERLAY, Overlay

__all__ = [
    "DerivedCatalogue",
    "SourceStatus",
    "custom_field_attributes",
    "derive_catalogue",
    "read_status",
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


#: Endings that are already singular, so a trailing "s" must not be stripped from them:
#: "chassis" is not the plural of "chassi", nor "status" of "statu".
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

    ``content_type`` is a Django label -- ``dcim.device``, not the endpoint. It is the
    schema component name lowercased, which :func:`derive_catalogue` records for each
    family it produces.
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
            if declared.get("type") in _SCALAR_TYPES:
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
        str(value_type) if value_type in _SCALAR_TYPES else "string",
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
    by_component = {family.component: family.id for family in families}
    custom_fields = tuple(custom_fields)

    warnings: list[str] = []
    derived: list[NetBoxObjectType] = []
    for family in families:
        derived.append(
            _derive_family(family, index, overlay, by_component, custom_fields, warnings)
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
    custom_fields: Sequence[Mapping[str, Any]],
    warnings: list[str],
) -> NetBoxObjectType:
    attributes: dict[str, Attr] = {}
    relationships: list[RelationshipMapping] = []

    for name, prop in index.properties(family.component).items():
        if not isinstance(prop, Mapping):
            continue
        if _is_noise(name) and (family.endpoint, name) not in overlay.keep_properties:
            continue
        attr, edge = _derive_property(name, prop, family, index, overlay, by_component, warnings)
        if attr is not None:
            attributes[name] = attr
        if edge is not None:
            relationships.append(edge)

    legacy_names = {legacy.name for legacy in overlay.legacy_attributes.get(family.endpoint, ())}
    for legacy in overlay.legacy_attributes.get(family.endpoint, ()):
        if legacy.name in attributes:
            warnings.append(
                f"{family.endpoint}: overlay declares legacy attribute {legacy.name!r}, "
                "but this schema serves it -- the declaration is no longer needed"
            )
            continue
        attributes[legacy.name] = Attr(legacy.path, legacy.json_type)

    attributes.update(
        custom_field_attributes(custom_fields, content_type=family.content_type(overlay))
    )

    presentation = overlay.presentation.get(family.endpoint)
    singular = _singularize(family.segment)
    # What this family calls itself. NetBox has no universal name property -- a prefix
    # is identified by ``prefix``, a circuit by ``cid``, an IP by ``address`` -- and the
    # same rule that reads a reference's natural key answers it here too.
    natural = index.natural_key(family.component)
    name_field = natural if natural in attributes else "display"
    return NetBoxObjectType(
        id=family.id,
        app=family.app,
        endpoint=family.endpoint,
        display_name=(presentation.display_name if presentation else None) or _titleize(singular),
        plural=(presentation.plural if presentation else None) or _titleize(family.segment),
        description=(presentation.description if presentation else None) or _describe(family),
        name_field=name_field,
        attributes=attributes,
        columns=(presentation.columns if presentation else ())
        or _default_columns(attributes, natural, legacy_names),
        search_fields=(presentation.search_fields if presentation else ())
        or _default_search_fields(attributes, natural),
        icon=presentation.icon if presentation else None,
        relationships=tuple(relationships),
    )


def _derive_property(
    name: str,
    prop: Mapping[str, Any],
    family: _Family,
    index: _SchemaIndex,
    overlay: Overlay,
    by_component: Mapping[str, str],
    warnings: list[str],
) -> tuple[Attr | None, RelationshipMapping | None]:
    nullable = bool(prop.get("nullable"))

    component = _referenced_component(prop)
    if component is not None:
        key = index.natural_key(component)
        key_type = index.properties(component).get(key, {}).get("type")
        target = by_component.get(_dereferenced_family(component))
        path = f"{name}.{key}"
        edge = (
            RelationshipMapping(rel=name, target_object_type=target, source=path)
            if target is not None
            else None
        )
        json_type = str(key_type) if key_type in _SCALAR_TYPES else "string"
        return Attr(path, json_type, nullable), edge

    choice = _choice(prop)
    if choice is not None:
        value_type, values = choice
        return Attr(f"{name}.value", value_type, nullable, values), None

    declared = prop.get("type")
    if declared in _SCALAR_TYPES:
        # A bare enum: the same choice set, rendered flat rather than as an object.
        return Attr(name, str(declared), nullable, _enum_of(prop)), None
    if declared == "array":
        return Attr(name, "array", nullable), None
    if declared == "object":
        return Attr(name, "object", nullable), None

    # No type at all: a generic reference, or free-form JSON. Only the overlay knows.
    if (family.endpoint, name) in overlay.untyped_objects:
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


def _describe(family: _Family) -> str:
    """A description for this family.

    Not NetBox's: the ``description`` on a component is the docstring of the *serializer*
    that renders it ("Adds support for custom fields and tags."), which describes the
    implementation rather than the objects. Overlay presentation is where a better one
    goes.
    """
    return f"NetBox {_titleize(family.segment).lower()}, as served by {family.endpoint}."


#: Properties worth a column on nearly any family, in the order they read well.
_PREFERRED_COLUMNS: tuple[str, ...] = ("name", "status", "role", "site", "tenant", "description")
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
    attributes: Mapping[str, Attr], natural: str, legacy: frozenset[str] | set[str]
) -> tuple[str, ...]:
    """Default browse columns.

    Never a legacy spelling. A column costs screen space whether or not it holds
    anything, so naming one a current NetBox has replaced leaves every operator a
    permanently blank column -- the defect fixed for ``ip_prefix`` by hand, made
    impossible here. Search fields are free of that cost and deliberately keep both
    spellings; see the plugin README.
    """
    chosen = [natural] if natural in attributes and natural not in legacy else []
    chosen += [
        name
        for name in _PREFERRED_COLUMNS
        if name in attributes and name != natural and name not in legacy
    ]
    return tuple(chosen) or ("name",)


def _default_search_fields(attributes: Mapping[str, Attr], natural: str) -> tuple[str, ...]:
    chosen = [natural] if natural in attributes else []
    chosen += [name for name in _PREFERRED_SEARCH if name in attributes and name != natural]
    return tuple(chosen) or ("name",)


def _stale_overlay_warnings(overlay: Overlay, endpoints: frozenset[str] | set[str]) -> list[str]:
    """Overlay entries naming an endpoint this document does not serve."""
    warnings: list[str] = []
    for endpoint, _ in overlay.legacy_attributes.items():
        if endpoint not in endpoints:
            warnings.append(f"overlay declares legacy attributes for unknown endpoint {endpoint}")
    for endpoint, _name in (*overlay.untyped_paths, *overlay.untyped_objects):
        if endpoint not in endpoints:
            warnings.append(f"overlay declares a generic-reference path for unknown {endpoint}")
    for endpoint in overlay.presentation:
        if endpoint not in endpoints:
            warnings.append(f"overlay declares presentation for unknown endpoint {endpoint}")
    for endpoint, name in overlay.keep_properties:
        if endpoint not in endpoints:
            warnings.append(f"overlay keeps {name!r} on unknown endpoint {endpoint}")
    return warnings
