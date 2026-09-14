<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# hegemony-inventory-netbox

NetBox inventory provider for **Hegemony**. Reads and queries **devices** and **sites**
from a NetBox instance over its REST API and feeds them into the core inventory.

It is an out-of-tree plugin: it depends only on
[`hegemony-inventory-sdk`](../../packages/inventory_sdk) and is discovered at runtime via
the `hegemony.inventory_plugins` entry-point group
(`netbox = "hegemony_inventory_netbox:register"`).

## Install

Provider wheels are **opt-in** — they are not bundled in the default images.
See the root
[Install From A Release](../../README.md#install-from-a-release) guide for Docker
commands, checksum verification, and local-wheel development installs.

## Configure

Create a `netbox` inventory provider (Settings → Inventory Providers, or
`POST /inventory/providers`). The provider form is schema-driven, so each field below is
rendered automatically and `token_ref` gets a secret picker.

| Field | Required | Default | Notes |
|---|---|---|---|
| `url` | yes | — | NetBox base URL, e.g. `https://netbox.example.com` |
| `token_ref` | yes | — | Secret **reference** to the API token, e.g. `{{ secret('vault://netbox/token') }}` — never the raw token |
| `auth_scheme` | no | `Bearer` | Authorization scheme prefixed to the resolved token |
| `auth_fallback_schemes` | no | `["Token"]` | Schemes retried on auth failure |
| `verify_tls` | no | `true` | Verify the TLS certificate |
| `device_query` | no | `""` | NetBox device filter applied to **every** device read, e.g. `status=active&role=wan-edge` |
| `site_tree_source` | no | `region` | Which NetBox hierarchy becomes the site tree: `region`, `site_group` or `none`. The other axis is still tagged |
| `tag_key_prefix` | no | `tag:` | Prefix for NetBox tag slugs surfaced as filterable device tags. Set to `""` for bare keys |
| `status_map` | no | built-in | Maps a NetBox device status slug to a maintenance state (`active`, `maintenance`, `disabled`) |
| `field_map` | no | built-in | Maps normalized inventory fields → NetBox REST payload paths, including optional `access_config.*` refs under `custom_fields` |
| `default_access_config` | no | `{}` | Access-config defaults applied to every device |
| `object_types` | no | `null` | A ceiling on which NetBox families this provider may read beyond devices and sites. `null` (or `["*"]`) = every family; list ids to put the rest out of reach. Normally left unset — the platform's own selection is the control. See [Object types](#object-types) |
| `object_queries` | no | `{}` | Per-family NetBox filter, e.g. `{"interface": "mgmt_only=false"}` — `device_query` for everything that is not a device |
| `query_cache_ttl_seconds` | no | `null` | Optional query cache TTL (0–86400 s) |
| `timeout_seconds` | no | `10` | Per-request timeout |

Minimal config:

```json
{
  "url": "https://netbox.example.com",
  "token_ref": "{{ secret('vault://netbox/token') }}"
}
```

Built-in `field_map` defaults:

```json
{
  "external_id": "id",
  "name": "name",
  "display_name": "display",
  "hostname": "hostname",
  "mgmt_host": "primary_ip4.address",
  "platform": "platform.slug",
  "model": "device_type.model",
  "vendor": "device_type.manufacturer.name",
  "serial": "serial",
  "asset_tag": "asset_tag",
  "rack": "rack.name",
  "position": "position",
  "tenant": "tenant.slug",
  "location": "location.slug",
  "mgmt_host_v6": "primary_ip6.address",
  "site.external_id": "site.slug",
  "site.name": "site.name",
  "role": "role.slug",
  "tags": "tags",
  "access_config.ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
  "access_config.ssh.password_ref": "custom_fields.hegemony_ssh_password_ref",
  "access_config.enable.password_ref": "custom_fields.hegemony_enable_password_ref",
  "access_config.ssh.private_key_ref": "custom_fields.hegemony_ssh_private_key_ref"
}
```

### Platform

The device `platform` is mapped from NetBox's native `platform.slug`. When a device has
no platform in NetBox, the provider emits no value and the **core inventory service**
applies its default platform during materialization — there is no per-provider platform
setting.

### Device lifecycle status

NetBox's device status is mapped onto the platform's maintenance state, so the source
of truth's view of a device's lifecycle travels with it:

| NetBox status | Maintenance state |
|---|---|
| `offline`, `decommissioning` | `disabled` |
| `planned`, `staged`, `failed`, `inventory` | `maintenance` |
| `active`, anything unmapped | *(left alone)* |

An unmapped status yields no value rather than forcing the device back to `active`, so a
locally-set state is not overwritten by a sync. Override the table per provider with
`status_map`; only `active`, `maintenance` and `disabled` are accepted as targets.

### Scoping which devices sync

Auto-sync calls `query_devices` with an empty expression, so by default a provider
imports the **entire** NetBox device table. `device_query` sets a baseline filter applied
to every device read:

```json
{ "device_query": "status=active&role=wan-edge&tag=managed" }
```

A caller-supplied query (a run-form preview, for instance) is appended to it, and NetBox
ANDs the two. Everything that is not a device is scoped the same way, per family, with
[`object_queries`](#scoping-each-family).

### Extra device fields

`vendor` maps from `device_type.manufacturer.name` and lands on the device row, where the
UI's Vendor column and `?vendor=` search filter can use it. `serial`, `asset_tag`, `rack`,
`position`, `tenant`, `location` and `mgmt_host_v6` have no device column, so they are
surfaced under `descriptive` — searchable (it is GIN-indexed) and visible over the API.
Each is omitted entirely when the device does not carry it.

### Linking back to NetBox

Every device, site and object of any read family carries `descriptive.provider_url`, the
NetBox UI page
for that object, so Hegemony can offer an "Open in NetBox" link. It is read from NetBox's
own `display_url`, falling back to the API `url` with its `/api` prefix dropped, and is
pinned to the configured provider origin — a NetBox echoing another host cannot turn the
link into an off-site redirect.

### Site identity

A site reaches the platform by two routes — the `/api/dcim/sites/` listing and the
`site` object nested inside each device — and the platform keys sites on
`(provider_id, external_id)`. Both routes therefore resolve the external id through the
**same** `field_map` entry, `site.external_id` (default `site.slug`, so the key is the
slug). Remapping it to `site.id` moves both routes together. Parent links use the same
key, so the NetBox site hierarchy survives the round trip.

### Tags

NetBox tags are flat slugs, but devices are filtered on a `key=value` map
(`tags[role]=core` in device search, `tag.*` in run forms). Each NetBox tag is surfaced
## The site tree

NetBox keeps **two** independent nested hierarchies above a site — **Region**
(geography) and **Site Group** (function) — and a site carries a nullable
foreign key to each. A Hegemony site has a single parent, so the two cannot both
be projected onto it: combining them would mean inventing combined nodes that
exist in neither system.

`site_tree_source` picks which one becomes the tree:

| Value | Effect |
|---|---|
| `region` (default) | Regions are materialized as sites, nested by their own parent; each site hangs from its region |
| `site_group` | The same, using site groups |
| `none` | No tree. Sites arrive flat, and the hierarchy endpoint is never fetched |

**The axis that loses is not lost.** Both are always surfaced as site tags,
`region` and `group`, so an operator can still select "every branch site" or
"everything in Gondor" whichever way the tree is drawn. They are bare keys rather
than `tag_key_prefix` ones, because they are structural NetBox fields rather than
NetBox tags — the same reason a device's `role` is bare.

Two details worth knowing:

- **Synthesized nodes carry a prefix** — `region:gondor`, `site-group:branch`.
  NetBox slugs are unique per model, not globally, so a region and a site may both
  be `gondor`; Hegemony keys sites on `(provider_id, external_id)`, and without the
  prefix those two would collide into one row. Nodes are also tagged
  `node_type=region` (or `site_group`), so a selector can tell an aggregation node
  from a place with equipment in it.
- **Only the ancestors of the sites you sync are emitted.** A NetBox region tree
  can describe an entire estate; a provider scoped with `device_query` has no
  business dragging all of it in.

NetBox **Locations** — the rooms and rack rows *inside* a site — are deliberately
not part of the tree. They arrive as a device attribute (`location`) instead,
because making them sites would move every device that has one a level down and
silently break any selector pinned to a site.

as `<tag_key_prefix><slug> = "true"` — the tag `core` becomes `tags["tag:core"]`. The
prefix keeps a NetBox tag named `role` from colliding with the mapped device role. The
raw slugs remain available unprefixed as `native_tags`.

### Access config

The same generic `field_map` also controls optional access-config refs. For example,
`access_config.ssh.username_ref` can point at a `custom_fields.*` path in the NetBox device
payload. Provider-level `default_access_config` is merged first, and mapped device-specific
values override those defaults when present.

## Object types

NetBox models far more than devices and sites, and this provider can read all of it. What
it reads for a given provider is that provider's choice, because reading everything is
rarely what anyone wants: an interface table is two orders of magnitude larger than a
device table, and a rack elevation is not worth a sync.

- `device`, `site` — always read. Devices and sites are the platform's own tables, with
  their own columns, targeting and site tree.
- Everything else is a **family** in the catalogue
  (`src/hegemony_inventory_netbox/object_types.py`), materialized into the generic
  inventory-object store with schema-driven list and detail pages, free-text search and
  filters, and no frontend code.

### Choosing what to read

**The choice is made in Hegemony, not here.** Every family in the catalogue is declared
as a supported resource, so the provider's *Object types to sync* selection in the
provider form offers all of them, and a provider reads the ones ticked there. A family
that is not ticked is not requested at all, and rows an earlier sync stored for it are
marked **stale** on the next sync rather than left looking current.

`object_types` is a *ceiling* above that selection, for the rare case where a family must
be out of reach whatever anyone ticks — a read-only token that must never touch the
interface table, say:

```json
{ "object_types": ["interface", "vlan", "virtual_machine", "circuit"] }
```

- Omit it, or leave it `null`, for no ceiling: every family the plugin knows is offered.
- `["*"]` is the same as unset, and stays true of families a later plugin version adds,
  because the wildcard is stored as written rather than expanded.
- `[]` allows devices and sites only.
- A comma-separated string works too (`"rack, cable"`), and an id nothing serves is
  rejected by name, with the valid ids listed.

A family has to be under the ceiling **and** selected in the provider form to be read, so
a narrowed ceiling makes the ticked-but-unreachable families in that form a lie. Leave it
unset unless you mean it.

### Scoping each family

`object_queries` is to a family what `device_query` is to devices — a NetBox filter
applied to every read of it:

```json
{
  "object_types": ["interface", "ip_address"],
  "object_queries": {
    "interface": "mgmt_only=false&enabled=true",
    "ip_address": "status=active"
  }
}
```

A caller-supplied query is appended, and NetBox ANDs the two. Keys are object type ids; a
key naming no family is rejected.

The filter bounds what the provider can see of that family, not just what it lists, so
reading one object obeys it too: a scoped family resolves an id through the filtered list
endpoint, and an object outside the scope reads as absent rather than being returned to a
caller that happens to know its id.

### The catalogue

50 families, grouped by NetBox app. Ids are stable: they are stored on every row and
referenced by authored flow forms.

| NetBox app | Families |
| --- | --- |
| `tenancy` | `tenant`, `tenant_group`, `contact` |
| `dcim` | `location`, `rack`, `manufacturer`, `device_type`, `device_role`, `platform`, `interface`, `console_port`, `power_port`, `power_outlet`, `front_port`, `rear_port`, `module`, `module_type`, `inventory_item`, `cable`, `power_panel`, `power_feed`, `virtual_chassis`, `virtual_device_context` |
| `ipam` | `ip_prefix`, `ip_address`, `vlan`, `ip_range`, `aggregate`, `rir`, `ipam_role`, `vlan_group`, `vrf`, `route_target`, `service`, `asn`, `fhrp_group` |
| `virtualization` | `virtual_machine`, `cluster`, `vm_interface` |
| `circuits` | `circuit`, `circuit_type`, `circuit_provider`, `provider_network`, `circuit_termination` |
| `wireless` | `wireless_lan`, `wireless_link` |
| `vpn` | `vpn_tunnel`, `l2vpn` |
| `extras` | `config_context`, `journal_entry` |

`circuit_provider` is NetBox's *Provider* model — a carrier. It is not called `provider`
because an inventory provider is something else entirely here.

Deliberately absent: **regions and site groups**, which this provider already projects
onto the platform's site tree and would otherwise exist twice; **device and module bays**
and **cable terminations**, which are structural placeholders rather than things anyone
targets; and NetBox's own plumbing — webhooks, scripts, jobs, object changes — which is
not inventory.

### Relationships

Families declare their edges, so a row points at what it belongs to: an interface at its
device and its LAG, a prefix at its VLAN and VRF, a circuit at its carrier, a VM at its
cluster. Edges cite the same external id the target row is keyed on — NetBox's numeric id
for objects and devices, the site slug for sites — so they line up with what a device or
site sync already stored.

Polymorphic references are stored as data rather than as edges: an IP address keeps
`assigned_object_type` and `assigned_object_id` as attributes, because one edge cannot
name a target type that varies per row.

### NetBox versions

Reads work across releases. Where NetBox replaced a specific foreign key with a generic
one, both spellings are mapped and whichever the server does not serve is simply null: a
prefix's and cluster's `site` alongside `scope`, a circuit termination's `site` and
`provider_network` alongside `termination`, a service's `device` and `virtual_machine`
alongside `parent` (4.2 and 4.3), and a contact's `group` alongside `groups`. The legacy
edge is emitted only when its field is present, so an older NetBox keeps the edge and a
current one carries the generic value as an attribute.

The **default browse columns** assume the generic shape, since that is what a supported
NetBox serves. On a pre-4.2 or pre-4.3 server the generic column reads empty while the
legacy value is still stored, still searchable, and still shown on the object's own page,
which lists every attribute. Listing both spellings as columns would instead leave every
operator one permanently empty column, whichever release they run.

## Adding an object type

One entry in `OBJECT_TYPES` in `src/hegemony_inventory_netbox/object_types.py`, and
nothing else:

```python
NetBoxObjectType(
    id="ike_policy",
    app="vpn",
    endpoint="/api/vpn/ike-policies/",
    display_name="IKE Policy",
    plural="IKE Policies",
    description="IKE policies referenced by IPsec profiles.",
    name_field="name",
    columns=("name", "version", "mode"),
    search_fields=("name", "description"),
    attributes={
        "name": _text("name", nullable=False),
        "version": _int("version"),
        "mode": _text("mode.value"),
        "description": _text("description"),
    },
)
```

The entry yields both halves of the wiring: the provider reads it through
`map_records`, and `register()` turns it into the `ObjectTypeSpec` the platform lists,
searches and renders it with. Field paths are dotted and a missing path is null rather
than an error, which is how one catalogue serves several NetBox releases.

After installing the new wheel version and restarting, the family appears in the
Objects page's type picker and in the provider form's type selection — no core change
and no database migration. It is read by any provider whose selection includes it.
