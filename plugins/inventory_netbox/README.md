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
| `tag_key_prefix` | no | `tag:` | Prefix for NetBox tag slugs surfaced as filterable device tags. Set to `""` for bare keys |
| `field_map` | no | built-in | Maps normalized inventory fields → NetBox REST payload paths, including optional `access_config.*` refs under `custom_fields` |
| `default_access_config` | no | `{}` | Access-config defaults applied to every device |
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

### Scoping what syncs

Auto-sync calls `query_devices` with an empty expression, so by default a provider
imports the **entire** NetBox device table. `device_query` sets a baseline filter applied
to every device read:

```json
{ "device_query": "status=active&role=wan-edge&tag=managed" }
```

A caller-supplied query (a run-form preview, for instance) is appended to it, and NetBox
ANDs the two.

### Extra device fields

`vendor` maps from `device_type.manufacturer.name` and lands on the device row, where the
UI's Vendor column and `?vendor=` search filter can use it. `serial`, `asset_tag`, `rack`,
`position`, `tenant`, `location` and `mgmt_host_v6` have no device column, so they are
surfaced under `descriptive` — searchable (it is GIN-indexed) and visible over the API.
Each is omitted entirely when the device does not carry it.

### Linking back to NetBox

Every device, site and IPAM object carries `descriptive.provider_url`, the NetBox UI page
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

### Access config

The same generic `field_map` also controls optional access-config refs. For example,
`access_config.ssh.username_ref` can point at a `custom_fields.*` path in the NetBox device
payload. Provider-level `default_access_config` is merged first, and mapped device-specific
values override those defaults when present.

## Supported object types

- `device`, `site` — core device/site inventory.
- `ip_prefix`, `ip_address`, `vlan` — NetBox IPAM prefixes/addresses and VLANs. They appear
  under the **Inventory** menu with schema-driven list/detail pages and sync alongside
  devices/sites whenever the provider syncs.

List and detail reads both go through the endpoint/mapping table, and a query mapping is
pushed to NetBox as a querystring rather than filtered client-side.

## Adding an object type

The IPAM/VLAN types are wired through two small tables, so adding another NetBox object type
(for example VRFs) is two edits plus a wheel release — no `list_objects` changes:

1. Add an endpoint → mapping entry to `_IPAM_OBJECT_SOURCES` in
   `src/hegemony_inventory_netbox/provider.py`:

   ```python
   "vrf": (
       "/api/ipam/vrfs/",
       ObjectFieldMapping(
           object_type="vrf",
           identity="id",
           name_field="name",
           field_map={"name": "name", "rd": "rd", "description": "description"},
       ),
   ),
   ```

2. Add a matching `ObjectTypeSpec` to `_OBJECT_TYPES` in
   `src/hegemony_inventory_netbox/__init__.py`, and add its id to `supported_resources`:

   ```python
   ObjectTypeSpec(
       id="vrf",
       display_name="VRF",
       plural="VRFs",
       field_schema={
           "type": "object",
           "properties": {"name": {"type": "string"}, "rd": {"type": ["string", "null"]}},
       },
       ui=ObjectTypeUIHints(columns=("name", "rd", "description")),
   ),
   ```

The provider's `list_objects` already dispatches any registered source through `map_records`,
so nothing else changes. After installing the new wheel version and restarting, the type
appears in the inventory submenu — no core change and no database migration.
