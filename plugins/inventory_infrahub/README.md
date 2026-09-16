<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# hegemony-inventory-infrahub

Infrahub inventory provider for **Hegemony**. Reads and queries **devices** and **sites**
from an [Infrahub](https://www.opsmill.com/) instance over its branch-aware GraphQL API and
feeds them into the core inventory.

It is an out-of-tree plugin: it depends only on
[`hegemony-inventory-sdk`](../../packages/inventory_sdk) and is discovered at runtime via
the `hegemony.inventory_plugins` entry-point group
(`infrahub = "hegemony_inventory_infrahub:register"`).

## Install

Provider wheels are **opt-in** — they are not bundled in the default images.
See the root
[Install From A Release](../../README.md#install-from-a-release) guide for Docker
commands, checksum verification, and local-wheel development installs.

## Configure

Create an `infrahub` inventory provider (Settings → Inventory Providers, or
`POST /inventory/providers`). The form is schema-driven; `token_ref` gets a secret picker.

| Field | Required | Default | Notes |
|---|---|---|---|
| `url` | yes | — | Infrahub base URL |
| `token_ref` | yes | — | Secret **reference** to the API token, e.g. `{{ secret('vault://infrahub/token') }}`. For compatibility across Infrahub deployments, raw token values are tried with `X-INFRAHUB-KEY` first and then common `Authorization` styles; if the resolved secret already starts with a known auth scheme like `Bearer`, that explicit scheme is tried first and the provider falls back when appropriate. |
| `verify_tls` | no | `true` | Verify the TLS certificate |
| `branch` | no | `main` | Infrahub branch to read from |
| `device_kind` | no | `InfraDevice` | GraphQL kind queried for devices |
| `field_map` | no | built-in | Maps descriptor fields → GraphQL value paths. The built-in defaults target the standard Infrahub schema; if your deployment adds custom access-config refs or a non-standard management-port field, map those explicitly here. |
| `default_access_config` | no | `{}` | Access-config defaults applied to every device |
| `timeout_seconds` | no | `10` | Per-request timeout |

Minimal config:

```json
{
    "url": "https://infrahub.example.com",
    "token_ref": "{{ secret('vault://infrahub/token') }}"
}
```

### Platform

The device `platform` is mapped from the configured `field_map` path
(default `platform.node.name.value`). When Infrahub has no platform for a device, the provider emits
no value and the **core inventory service** applies its default platform during
materialization.

### Access config

Optional access-config refs can be mapped through `field_map` keys such as
`access_config.ssh.username_ref` or `access_config.enable.password_ref`. Provider-level
`default_access_config` is merged first, and mapped device-specific values override those
defaults when present.

## Supported object types

`device`, `site`.

## Adding an object type

Object types are extended in code (edit + release the wheel):

1. Register the type and add its id to `supported_resources` in `register()`
   (`src/hegemony_inventory_infrahub/__init__.py`), declaring an `ObjectTypeSpec`.
2. Serve it from `list_objects` — run a GraphQL query for the new kind, then map each node
   into descriptors with the SDK helper:

   ```python
   from hegemony_inventory_sdk import ObjectFieldMapping, map_records

   _PREFIX = ObjectFieldMapping(
       object_type="ip_prefix",
       identity="id",
       name_field="prefix.value",
       field_map={"prefix": "prefix.value", "description": "description.value"},
   )

   async def list_objects(self, object_type, query=None, *, limit=None, cursor=None, context):
       if object_type == "ip_prefix":
           nodes = await self._graphql_nodes("IpamPrefix", context)  # provider helper
           return map_records(nodes, _PREFIX, provider_id=self.id)
       return await super().list_objects(
           object_type, query, limit=limit, cursor=cursor, context=context
       )
   ```

After installing the new wheel version and restarting, the type appears in the inventory
submenu with schema-driven list/detail pages — no core change and no database migration.
