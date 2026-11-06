<!-- SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o. -->
<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# Generic Git inventory objects

A repository defines its own object families. There is no built-in type catalogue
and no dependency on the NetBox plugin. Every family uses the existing inventory
SDK, synchronization, object browser, and `inventory.objects` form option source.

## Layout

Under the provider's configured `path`, add `object-types/*.yaml` definitions and
`objects/<type-id>/*.yaml` records. Both `.yaml` and `.yml` are accepted. Legacy
`sites/` and `devices/` formats remain unchanged, including their identities,
hierarchy, access configuration, and secret references. They are not copied to the
generic object store. A repository may contain only generic objects.

See [the complete example](examples/generic-objects/inventory) for custom scopes,
services, VLANs, and prefixes. The latter two are ordinary definitions, not special
cases in the plugin. Add another type by adding YAML, without editing Python.

## Definitions and records

Definitions use `schema_version: 1`, `kind: object_type`, `id`, `display_name`, and
`field_schema`. Optional fields are `plural`, `description`, `group`, `relationships`,
`ui`, and `filters`. Records use `schema_version: 1`, `kind: object`, `object_type`,
`external_id`, `name`, optional `display_name`, `attributes`, and `relationships`.
Unknown envelope properties and unsupported versions are rejected.

IDs follow the SDK's 1–100 character lowercase letter/digit/dot/underscore/hyphen
rule and begin with a letter or digit. Prefer an author-owned prefix such as
`example.service`. `device` and `site` are reserved. Filenames do not determine
generic identity: renaming a file or object name retains the same stored object.
Changing the external ID creates a new object and stales the old one. External IDs
are nonblank strings unique within one provider and type.

`field_schema` is a JSON Schema object describing `attributes`. Supported keywords:
`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`,
`minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `minLength`,
`maxLength`, `minItems`, `maxItems`, `minProperties`, `maxProperties`, `title`,
`description`. Every schema node declares `type`; nullable unions are supported.
Strings, booleans, integers, numbers, arrays, nested objects, and declared nulls
retain their JSON types. There is no string-to-number coercion. Quote dates in YAML.
References, regex patterns, format validators, combinators, custom code, and
non-finite numbers are unsupported and rejected. Prefix strings are not checked
for CIDR syntax in this version.

Documents are bounded to 32 nesting levels and 100,000 expanded JSON values.
Existing host file-count and per-file byte limits apply across legacy and generic
files together. Symlinks, duplicate YAML keys, duplicate identities, unsafe paths,
and excessively deep/large directory traversals fail validation.

## Display and filters

`ui` accepts `icon`, `columns`, `detail_layout`, `search_fields`, and `default_sort`.
Field names refer to top-level attributes or `name`, `display_name`, `external_id`.
A leading `-` reverses default sorting. Nested values display as structured data;
nested paths are not filterable/searchable columns in this version.

`filters` lists top-level scalar attributes supporting provider equality filters
as `attr.<name>`. Provider reads also support exact `name`, `external_id`, and
case-insensitive `q` search. Unknown or ill-typed filters fail closed. Provider
reads preserve strict JSON types (a boolean is not an integer). Object browsing
and dropdowns retain the platform's existing SQL filtering/value semantics.
Enumeration is deterministic, in pages of 500, with the host page-count limit.
`list_objects` supports `limit`; explicit cursors are rejected. Use
`iter_object_pages` for complete enumeration.

## Relationships

Declare each relation with `rel`, `target_object_type`, optional `label`, `many`
(default false), and `required` (default false). Records supply a list of edges:

```yaml
relationships:
  - rel: scope
    target_object_type: example.scope
    target_external_id: east
```

Targets are in the same provider. Forward references and cycles are supported;
missing targets, duplicate edges, undeclared relations, and cardinality violations
fail validation. Every target type must be declared in the repository, even if it
is not selected for synchronization. An unselected target may not have a stored
row to open in the UI. Cross-provider edges and edges to legacy site/device tables
are not supported. For a legacy site cascade, store its external ID in an explicit
attribute and use the matching form value; do not use its database UUID as though
it were the external ID. Relationships do not implicitly create filter attributes.

## Discovery, sync, and schema changes

Discover the configured provider to populate the existing object-type selection,
or let sync discover it. Each sync parses and validates one locked checkout into
memory, then uses that snapshot for discovery, legacy reads, and generic pages.
The object source metadata records its Git commit. Discovery's source version is
a deterministic definition fingerprint, so record-only edits do not change it.

Malformed snapshots do not replace the last valid schema and do not emit partial
records. Each type retains the platform's transaction/rollback boundary. A failed
read cannot stale that type's previously valid objects. Successful empty types
and removed records follow normal stale marking; deselection uses platform policy.
A declared type requires its object directory; commit `.gitkeep` for an empty type.
Remove both its declaration and directory to remove a family on rediscovery.
Removing all extension directories returns to legacy-only inventory.

A changed schema must validate its records before adoption. The same visible type
ID from multiple providers must have an identical schema and presentation contract
when Git is involved. Conflicts are rejected; choose a different ID for different
semantics. This protects unrelated registered/discovered schemas without requiring
any relationship to NetBox.

## Flow inputs

[The interface graph example](examples/generic-objects/interface-graph.yaml) defines
Scope → Port: scope submits its external ID, then `attr.scope` filters services and
port submits `attr.port`. It is a native interface graph, not a whole flow bundle.
The same structure can select VLANs by changing the type and value to
`example.vlan` and `attr.vid`.

```yaml
option_source:
  kind: inventory.objects
  config:
    object_type: example.service
    value_field: attr.port
```

Use an `options_filter` edge with `value: attr.scope` from the scope field to the
service field. Add the existing provider filter when a form must target a specific
provider. Supported submitted values are `id`, `external_id`, `name`, and
`attr.<top-level-attribute>`. The existing option source serializes scalar values
as strings: port 443 submits `"443"`. Convert in the flow when an integer is needed.
Missing/empty option values are omitted; duplicate values collapse into one option.
Use `id` when selections must identify a unique object across providers. Stale
objects are excluded by default. Submission revalidates selections against the
current parent filter; a value from another scope is rejected.

## Compatibility and release

The SDK data contracts remain at ABI 1 with no SDK API extension. Generic objects
require the companion platform change providing `git.locked_workdir`, Git schema
refresh, and discovered-field dropdown search. On older hosts this plugin still
reads legacy-only repositories, but refuses generic repositories with an actionable
compatibility error. Deploy the platform support before enabling generic files.
No minimum released platform version is claimed until the changes are released.
Use the repository's unified release workflow to assign versions and publish wheels;
this feature does not change the SDK pin independently.
