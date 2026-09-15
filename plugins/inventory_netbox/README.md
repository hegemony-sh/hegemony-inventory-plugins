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
| `schema_discovery` | no | `false` | Have the connection test also ask this NetBox what it serves — see [Ask an instance what it has](#ask-an-instance-what-it-has). Off by default: the schema is several megabytes |
| `device_query` | no | `""` | NetBox device filter applied to **every** device read, e.g. `status=active&role=wan-edge` |
| `site_tree_source` | no | `region` | Which NetBox hierarchy becomes the site tree: `region`, `site_group` or `none`. The other axis is still tagged |
| `tag_key_prefix` | no | `tag:` | Prefix for NetBox tag slugs surfaced as filterable device tags. Set to `""` for bare keys |
| `status_map` | no | built-in | Maps a NetBox device status slug to a maintenance state (`active`, `maintenance`, `disabled`) |
| `field_map` | no | built-in | Maps normalized inventory fields → NetBox REST payload paths, including optional `access_config.*` refs under `custom_fields` |
| `default_access_config` | no | `{}` | Access-config defaults applied to every device |
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
as `<tag_key_prefix><slug> = "true"` — the tag `core` becomes `tags["tag:core"]`. The
prefix keeps a NetBox tag named `role` from colliding with the mapped device role. The
raw slugs remain available unprefixed as `native_tags`.
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

- `device`, `site` — always read. They are families in the catalogue like anything else,
  and the platform's devices and sites tables are *written from* the rows they store
  rather than fetched a second time. See [Devices and sites](#devices-and-sites).
- Everything else is a **family** in the catalogue
  (`src/hegemony_inventory_netbox/object_types.py`), materialized into the generic
  inventory-object store with schema-driven list and detail pages, free-text search and
  filters, and no frontend code.

### Choosing what to read

**The choice is made in Hegemony, not here — and there is no setting for it in this
config.** Every family in the catalogue is declared as a supported resource, so the
provider's *Object types to sync* selection in the provider form offers all of them, and
a provider reads the ones ticked there. A family that is not ticked is not requested at
all, and rows an earlier sync stored for it are marked **stale** on the next sync rather
than left looking current.

This plugin briefly carried its own `object_types` setting as a second, narrower say. It
is gone: the platform can only subtract from what a provider declares it serves, so the
two controls could disagree, and when they did the quieter one won — ticking **Racks**
synced no racks and the form gave no hint why. A config still carrying the key validates
and the key is ignored, so a provider configured while it existed needs no edit.

Which families are worth reading is still a real question — an interface table is two
orders of magnitude larger than a device table — but it is asked once, in the form.

### Scoping each family

`object_queries` is to a family what `device_query` is to devices — a NetBox filter
applied to every read of it:

```json
{
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

<!-- begin generated catalogue -->

104 families across 8 NetBox apps. Each family carries its
app as its `ObjectTypeSpec.group`, which is how the platform's *Object types to
sync* selection shows them as a tree rather than a hundred flat checkboxes.

| NetBox app | Families |
| --- | --- |
| `circuits` | `circuit`, `circuit_provider`, `circuit_termination`, `circuit_type`, `circuits.circuit_group_assignments`, `circuits.circuit_groups`, `circuits.provider_accounts`, `circuits.virtual_circuit_terminations`, `circuits.virtual_circuit_types`, `circuits.virtual_circuits`, `provider_network` |
| `dcim` | `cable`, `console_port`, `dcim.cable_terminations`, `dcim.console_port_templates`, `dcim.console_server_port_templates`, `dcim.console_server_ports`, `dcim.device_bay_templates`, `dcim.device_bays`, `dcim.front_port_templates`, `dcim.interface_templates`, `dcim.inventory_item_roles`, `dcim.inventory_item_templates`, `dcim.mac_addresses`, `dcim.module_bay_templates`, `dcim.module_bays`, `dcim.module_type_profiles`, `dcim.power_outlet_templates`, `dcim.power_port_templates`, `dcim.rack_reservations`, `dcim.rack_roles`, `dcim.rack_types`, `dcim.rear_port_templates`, `device`, `device_role`, `device_type`, `front_port`, `interface`, `inventory_item`, `location`, `manufacturer`, `module`, `module_type`, `platform`, `power_feed`, `power_outlet`, `power_panel`, `power_port`, `rack`, `rear_port`, `region`, `site`, `site_group`, `virtual_chassis`, `virtual_device_context` |
| `extras` | `config_context`, `extras.config_templates`, `extras.object_types`, `extras.tagged_objects`, `extras.tags`, `journal_entry` |
| `ipam` | `aggregate`, `asn`, `fhrp_group`, `ip_address`, `ip_prefix`, `ip_range`, `ipam.asn_ranges`, `ipam.fhrp_group_assignments`, `ipam.service_templates`, `ipam.vlan_translation_policies`, `ipam.vlan_translation_rules`, `ipam_role`, `rir`, `route_target`, `service`, `vlan`, `vlan_group`, `vrf` |
| `tenancy` | `contact`, `tenancy.contact_assignments`, `tenancy.contact_groups`, `tenancy.contact_roles`, `tenant`, `tenant_group` |
| `virtualization` | `cluster`, `virtual_machine`, `virtualization.cluster_groups`, `virtualization.cluster_types`, `virtualization.virtual_disks`, `vm_interface` |
| `vpn` | `l2vpn`, `vpn.ike_policies`, `vpn.ike_proposals`, `vpn.ipsec_policies`, `vpn.ipsec_profiles`, `vpn.ipsec_proposals`, `vpn.l2vpn_terminations`, `vpn.tunnel_groups`, `vpn.tunnel_terminations`, `vpn_tunnel` |
| `wireless` | `wireless.wireless_lan_groups`, `wireless_lan`, `wireless_link` |

<!-- end generated catalogue -->

Ids are stable: they are stored on every synced row and referenced by authored flow
forms, so the fifty this plugin promised before the catalogue was generated keep their
bare spelling (`interface`, `ip_prefix`). Everything else is named after its endpoint
(`vpn.tunnel_groups`), which needs no choosing and cannot collide — that is what lets a
family nobody wrote down in advance, a NetBox plugin's models included, be named at all.

The table above is generated by `scripts/generate_catalogue.py` along with the catalogue
itself, so it cannot drift from what the plugin actually offers.

`circuit_provider` is NetBox's *Provider* model — a carrier. It is not called `provider`
because an inventory provider is something else entirely here.

**Regions and site groups** are in the catalogue, and so are **devices and sites**.
They used to be left out, because the provider listed them separately and offering them
again as objects would have stored each one twice. They are read once now, as families
like any other, and the platform's devices and sites tables are written from the rows
that read stored — see [Devices and sites](#devices-and-sites).

Deliberately absent: **device and module bays** and **cable terminations**, which are
structural placeholders rather than things anyone targets; and NetBox's own plumbing —
webhooks, scripts, jobs, object changes — which is not inventory.

### Devices and sites

A NetBox device used to arrive twice: once through `query_devices`, with its site nested
inside it and keyed by slug, and once as part of the `/api/dcim/sites/` listing, keyed by
the numeric id. Both were stored, so one NetBox site became two platform rows — and the
devices attached to the one with no parent link, which quietly disconnected the site tree
from the devices hanging off it. Two reads, two keys, and nobody's job to keep them
agreeing.

There is one read now. `device`, `site` and `region` are families, synced into the object
store like `interface` or `vlan`, and each carries a **projection binding** saying what
the platform should write from those rows:

| Family | Written to | Notes |
|---|---|---|
| `device` | `devices` | name, management address, vendor, model, platform, serial, secret refs, and the NetBox status mapped onto `maintenance_state` |
| `site` | `sites` | keyed on the slug, parented by its `region` edge |
| `region` | `sites` | keyed `region:<id>`, parented by its own `parent` edge |

Three things follow, and each is a behaviour rather than an implementation detail:

- **A site exists once.** A device reaches its site through an edge citing the same
  external id the site row is stored under, so there is one key and one row.
- **The hierarchy is real.** A region node is the projection of a stored region, not a
  synthetic site invented to give the tree something to hang from. The `region:` prefix
  is what keeps a region and a site of the same slug apart, since NetBox slugs are unique
  per model rather than globally.
- **Staleness follows the object.** A projected row is written from an active object, so
  the pass that marks an object stale is the pass that stops projecting it.

An operator's local columns — `maintenance_state_override`, `tags_local`, the device
overlay — are never written here. Projection owns the provider-sourced columns and
nothing else, exactly as the old device path did.

A provider that has never been asked to describe itself has no bindings, and answers for
its devices and sites the way it always did. Projection starts at the first discovery.

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

**Search fields go the other way and list both spellings.** A search field costs no screen
space, and an absent attribute simply never matches, so naming both makes the value
findable on either side of the rename while naming one leaves it unfindable on the other.
That is why an interface is searchable by `mac_address` and by `primary_mac_address`.

Both rules are enforced, not conventions. `_VERSION_REPLACED_FIELDS` in
`tests/api/test_netbox_object_types.py` lists every replaced pair and
`_REPLACED_FIELD_COLUMNS` lists the families that browse one, and parametrized tests fail
if a column names a dropped spelling, if a family that browses a replaced field stops
showing the generic one, or if search offers only one spelling of a pair.

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

## Deriving the catalogue from a NetBox schema

Every NetBox serves `/api/schema/`: a complete OpenAPI description of the API it is
actually running, generated from its own models. Almost everything the catalogue above
states by hand is already in there — which families exist, which app each belongs to,
what properties they carry, their types, their nullability, their choice sets — and it
is right about the release in front of it rather than the release someone had in mind.

`src/hegemony_inventory_netbox/derive.py` reads that document and produces
`NetBoxObjectType` entries from it. **The catalogue above is its output**, generated from
a recorded NetBox 4.3 schema by `scripts/generate_catalogue.py` and checked in. What is
still written by hand is `overlay.py` and nothing else.

### Why a checked-in catalogue exists at all

The obvious question, given that the provider can derive a catalogue from a live
instance: why ship one?

Because deriving needs three things the plugin does not always have — a configured
provider, network reach to it, and someone to have asked. The catalogue is what the
plugin offers before any of those:

- **Registration happens at import.** The `hegemony.inventory_plugins` entry point
  registers every object type when the plugin loads, long before a provider exists or a
  database is reachable. A catalogue fetched over HTTP could not be there in time.
- **Discovery is per provider and opt-in.** It runs when an operator presses *Discover*,
  and what it finds is stored against that one provider. A fresh install has none, and
  the provider form still has to offer something to tick.
- **A live NetBox may be unreachable.** The catalogue is also the fallback when discovery
  has never succeeded — an expired token or a proxy that strips `/api/schema/` should
  degrade to "the families NetBox generally has", not to nothing.

So the two are a seed and an answer, not two copies of one thing. A provider that has
described itself uses what *its* instance said; everything else uses the seed. The seed
being generated rather than typed is what keeps them consistent: both come out of the
same deriver.

### Why it is generated rather than checked

The deriver began as a way to *check* a hand-written catalogue: derive from a recorded
schema, compare, and treat every difference as a bug in one side or the other. That
comparison found six defects. It also made the hand-written table redundant — a file
that has to agree with a generator, checked by a test, is a file the generator should
simply write.

So it does, and the gate changed with it. There is nothing left to compare; what
`tests/api/test_netbox_derive.py` holds instead is that the checked-in catalogue is what
the current deriver and overlay produce, and that regeneration never drops an id this
plugin already promised.

The defects the comparison found while it still had two sides, all silent:

| Family | Field | Was | Is |
|---|---|---|---|
| `cable` | `type` | `type.value` | `type` |
| `fhrp_group` | `protocol` | `protocol.value` | `protocol` |
| `fhrp_group` | `auth_type` | `auth_type.value` | `auth_type` |
| `journal_entry` | `created_by` | declared a string | declared an integer |

The first three are one mistake made three times. NetBox renders most choice fields as
a `{value, label}` object, so `status.value` is right — but it renders *some* flat, as a
bare string, and reading `.value` from one of those yields `None` on every record
forever. Nothing in NetBox's API reference distinguishes the two shapes; the schema
does. `cable.type` and `fhrp_group.protocol` were browse columns, so the symptom was a
permanently blank column — the same symptom as the `ip_prefix` fix above.

The fourth is smaller: NetBox serves a journal entry's author as a numeric user id, and
the catalogue declared it a string.

### What generating it changed

| | Hand-written | Generated |
|---|---|---|
| Families | 50 | **104** |
| Attributes | 328 | **1310** |
| Relationships | 51 | **211** |
| Attributes carrying an exact choice set | 0 | **hundreds** |

Every family the hand-written catalogue offered is still offered, under the same id.
The rest are the collection endpoints a stock 4.3 serves that nobody had got round to
writing down, minus NetBox's own plumbing.

The attribute count is the trade the deriver makes: it stores what NetBox serves rather
than what someone selected, so a family is larger and never narrower. The choice sets
are the part no hand-written entry could keep — thirty status and type lists correct by
hand is not work anyone would choose, and without them the platform can only offer a
text box where it could offer a filter.

Browse columns are the one place a generated catalogue has to work for its living. The
derived default reads the first few fields in the order **NetBox's own serializer**
declares them, which is an editorial choice NetBox already made: identifying fields
first, bookkeeping last. Measured against the columns a person had chosen by hand for
the fifty families, it recovers 90% of them.

### Object-type ids are app-qualified

Derived ids are `dcim.interfaces`, not `interface`. The endpoint already is unique, so
qualifying by app needs no table of exceptions and no invented names — which is what
lets the deriver name families nobody wrote down in advance, a NetBox plugin's models
included. A bare singular cannot: NetBox serves interfaces under both `dcim` and
`virtualization`, and roles under both `dcim` and `ipam`.

The plugin still **registers** the hand-chosen ids. `LEGACY_OBJECT_TYPE_IDS` in
`object_types.py` publishes the mapping between the two, derived from the catalogue so
it cannot go stale. Adopting the derived spelling is not a rename of a constant: an
object-type id is stored in `inventory_provider_configs.object_types`, in the
`object_type` of every synced row, and inside the option sources of user-authored flow
definitions, so it means migrating that data and accepting both spellings while it
happens. The table is what makes that mechanical when it is worth doing.

### What stays hand-written

`overlay.py`, and only that. Every entry is a claim the schema does not make:

| Table | Size | Why the schema cannot say |
|---|---|---|
| `NATURAL_KEYS` | 7 | Which property of a referenced object to store. NetBox marks none of them. |
| `UNTYPED_PATHS` | 17 | Generic references carry no type at all — `Prefix.scope` is `{"nullable": true, "readOnly": true}` and nothing more. |
| `UNTYPED_OBJECTS` | 8 | Free-form JSON has the same untyped shape as a generic reference. |
| `REPLACED_FIELDS` | 9 | A schema describes one release; the shipped catalogue serves a range. |
| `EDGE_KEYS` | 1 | Which property identifies a family when something points at it. |
| `KEEP_PROPERTIES` | 1 | A virtual chassis's member count is the fact, not a counter about other objects. |
| `CONTENT_TYPES` | 0 | Reserved: a NetBox plugin may name endpoints the singularisation rule cannot reach. |
| `SKIP_ENDPOINTS` | 24 | NetBox's own plumbing is not inventory. |

Everything else is derived, including the rules the written catalogue had to be
corrected into following: a derived browse column never names a legacy spelling, and a
derived name field is the property someone *sets* rather than one NetBox computes, so a
prefix is named by `prefix` and not by how many child prefixes it has.

`REPLACED_FIELDS` is one table because the three rules that follow from it must not
drift apart. From the same nine pairs the deriver decides which spelling to synthesise
on a release that lacks it, which to keep out of the browse columns, and which to make
searchable — both, always, because a search field costs no screen space and an absent
attribute simply never matches.

### An attribute and an edge want different things

The same reference feeds two of them, and they disagree on purpose. The **attribute**
stores what a person reads: a VLAN's number, a manufacturer's name, a site's slug. The
**edge** has to cite the target's *external id*, because that is the key the platform
stored the target under and the only value that resolves back to it. So a prefix's VLAN
reads `vlan.vid` as an attribute and `vlan.id` as an edge. `EDGE_KEYS` holds the one
exception: the platform materialises sites through the site contract, which keys them on
their slug.

One more thing has to line up for an edge to resolve at all. `/api/dcim/devices/` returns
rows of a serializer NetBox calls `DeviceWithConfigContext`, while every reference *to* a
device resolves to `BriefDevice`. The deriver indexes a family under its undecorated name
as well, so the two meet; without that a device arrives with every attribute and nothing
pointing at it — no interface, no port, no module.

On a stock NetBox 4.3 the deriver emits **no warnings at all**, which is what makes a
warning worth reading: it means that instance serves a shape the overlay has not seen —
a later release, or a plugin's own model.

### Custom fields

`custom_field_attributes` turns an `/api/extras/custom-fields/` listing into stored
attributes under `custom_fields.<name>`. This is the part no shipped catalogue can ever
cover: every NetBox has custom fields, no two have the same ones, and the written
catalogue maps not one of them.

### The connection test says which NetBox answered

Testing a provider now reads `/api/status/` rather than listing one device. Both need a
valid token, but the status endpoint also reports the release and every installed
plugin, and it returns a few hundred bytes rather than several megabytes. The result
appears in the connection test's details as `netbox_version`, `netbox_full_version` and
`netbox_plugins`.

That is also the cheap half of discovery: a schema is only valid for the version that
produced it, so the version is what you ask for first.

### Ask an instance what it has

Everything above measures the deriver against a *recorded* schema. Set
`schema_discovery` and the connection test measures it against the NetBox in front of
you: it fetches that instance's `/api/schema/` and `/api/extras/custom-fields/`, derives
a catalogue from them, and reports the difference alongside the release and plugin list.

| Detail | What it answers |
|---|---|
| `netbox_families_served` | How many collection endpoints this instance serves |
| `netbox_families_offered` | How many of the catalogue's families it serves, so they can actually sync |
| `netbox_families_missing` | **Catalogue families it does not serve.** Ticking one of these is how an object type comes to sync nothing at all, and nothing said so before the first empty sync |
| `netbox_families_undeclared` | Families it serves that the catalogue does not offer — its plugins' models among them |
| `netbox_custom_fields` | Custom fields defined on it. No shipped catalogue can carry these |
| `netbox_schema_warnings` | Shapes the deriver did not recognise. Empty on a stock 4.3, so anything here is a later release or a plugin model worth teaching the overlay about |

It is **off by default** because the schema is several megabytes on a stock install, where
a connection test is otherwise a few hundred bytes, and because nothing else reads it yet.

It is also **best-effort**. The connection it reports on has already succeeded by the time
the schema is fetched, so a schema that cannot be read — an older release, a proxy that
strips the endpoint, an instance large enough to time out — costs the extra detail and
nothing else. A working provider never fails its connection test because discovery did.

### Discovery as a contract

`schema_discovery` above reports the gap. `discover()` returns the description itself,
as the SDK's `DiscoveryResult` — every object type this instance serves, each with the
mapping to read it, plus the release that produced the answer. The provider declares
`Capability.DISCOVER` so the platform knows it can ask.

It **extends** the shipped catalogue rather than shadowing it. A family the catalogue
already knows keeps the id this plugin registers (`interface`, not `dcim.interfaces`);
only families it does not — a later release's, a NetBox plugin's — arrive under the
derived app-qualified id. Relationship targets are re-keyed the same way, so an
interface discovered on a live instance still points at `device`. Without that rule an
instance would offer two object types for every family, and every row already synced
under the registered id would be stranded.

Devices and sites come back like any other family, each with a **projection binding**
saying which platform table it is written to and which attribute fills which column.
They used to be left out, because the platform fetched them separately and offering them
again would have stored every device twice. One read and one key is what removes that,
not a rule about which families may be described — see [Devices and
sites](#devices-and-sites).

A binding is sent only for a family this instance actually serves, so an instance
without regions gets a flat site list rather than a binding that projects from rows
which never arrive.

Where the connection test's probe is best-effort, `discover()` raises. The caller asked
for the description, and there is nothing to return in its place.

### Regenerating the fixture

`tests/fixtures/netbox-4.3-schema.json` is a real document, reduced to what the deriver
reads. Nothing in it was written to make the tests pass, which is the only reason the
conformance test means anything.

```console
curl -H 'Accept: application/json' https://netbox.example/api/schema/ > schema.json
python scripts/trim_netbox_schema.py schema.json tests/fixtures/netbox-4.3-schema.json
```

### What this does not do yet

The deriver does not run at sync time. A provider reads the catalogue it shipped, or
what the platform stored when discovery last ran — never a schema fetched during the
sync itself, which would put a multi-megabyte read in front of every pass.

Nothing is written back. `discover()` describes; it does not create, update or delete
anything in NetBox.
