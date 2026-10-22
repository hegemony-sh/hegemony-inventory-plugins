<!--
SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# hegemony-inventory-git

Git-backed YAML inventory provider for **Hegemony**. Reads **devices** and **sites** from
plain YAML files in a Git repository you already manage, and feeds them into the core
inventory.

It is an out-of-tree plugin: it depends only on
[`hegemony-inventory-sdk`](../../packages/inventory_sdk) and is discovered at runtime via
the `hegemony.inventory_plugins` entry-point group
(`git = "hegemony_inventory_git:register"`).

## Install

Provider wheels are **opt-in** — they are not bundled in the default images.
See the root
[Install From A Release](../../README.md#install-from-a-release) guide for Docker
commands, checksum verification, and local-wheel development installs.

## How it works

The Git provider reads inventory files from a Git checkout. You
first register a **Git repository** under *Settings → Git Repositories* (URL + branch +
credentials), and the Git inventory provider then points at a directory **inside** that
repository. The repository fetch, credentials, caching, and SSRF protection are all handled
by the platform's existing Git machinery — the plugin only parses the YAML it is handed.

## Configure

Create a `git` inventory provider (Settings → Inventory Providers, or
`POST /inventory/providers`). The form is schema-driven; `git_repo_id` renders as a dropdown
of the Git repositories you have added.

| Field | Required | Default | Notes |
|---|---|---|---|
| `git_repo_id` | yes | — | The configured Git repository to read from (Settings → Git Repositories) |
| `branch` | no | `main` | Branch to read |
| `path` | yes | — | Repo-relative path to the **inventory directory** (the one containing `sites/` and `devices/`) |
| `default_access_config` | no | `{}` | Access-config defaults applied to every device |
| `refresh_strategy` | no | `cache_ttl` | Cache strategy (currently the only value) |

Example config:

```json
{
  "git_repo_id": "<uuid of a configured Git repository>",
  "branch": "main",
  "path": "inventory"
}
```

## Inventory file structure

`path` points at a **directory**. Inside it, sites and devices live in their own
subdirectories, one record per file:

```text
<path>/
  sites/
    emea/
      emea.yaml             # parent/root site "emea"
      nl/
        nl.yaml             # nested parent site "emea/nl"
        ams01.yaml          # leaf site "emea/nl/ams01"
  devices/
    <external_id>.yaml      # one device per file; file stem MUST equal external_id
    ...
```

Rules enforced by the loader (fail-closed — any error rejects the whole sync):

- Files use `.yaml` or `.yml`; each is a single YAML document (no multi-doc).
- The loader scans `sites/` recursively. A site file's stem must equal its local
  `external_id`; a device file's stem must equal its `external_id`. Duplicate **full site
  paths**, duplicate device `external_id`s, and duplicate YAML keys are rejected.
- Symlinked files/directories are not allowed, and `path` may not escape the repository.
- A site with children is represented as `<external_id>/<external_id>.yaml`; a leaf site may
  stay as `<external_id>.yaml` directly under its parent directory.
- The site's full `external_id` is derived from its path. Examples:
  - `sites/emea/emea.yaml` → `emea`
  - `sites/emea/nl/nl.yaml` → `emea/nl`
  - `sites/emea/nl/ams01.yaml` → `emea/nl/ams01`
- Devices reference sites by that full derived path (for example `emea/nl/ams01`); unknown
  site references and missing parent sites are rejected.
- Duplicate local site ids or names are allowed in different branches because the full path is
  the unique identity.

A ready-to-copy example lives in [`examples/`](examples/) (two devices + three nested sites).
Copy its contents into your repository and set `path` to wherever you put it.

### Site fields (`sites/.../<external_id>.yaml`)

| Field | Required | Default | Notes |
|---|---|---|---|
| `schema_version` | yes | — | must be `1` |
| `kind` | yes | — | must be `site` |
| `external_id` | yes | — | stable id for the **current path segment**; **must equal the file stem** |
| `name` | yes | — | site name |
| `description` | no | — | human-readable description |
| `location` | no | — | address or location summary |
| `tags` | no | `{}` | key/value tags stored on the site |
| `custom_fields` | no | `{}` | free-form metadata exposed as safe descriptive data |

The provider derives the site's full `external_id` and `parent_external_id` from the nested
path, so those values are **not** written into the YAML.

### Device fields (`devices/<external_id>.yaml`)

| Field | Required | Default | Notes |
|---|---|---|---|
| `schema_version` | yes | — | must be `1` |
| `kind` | yes | — | must be `device` |
| `external_id` | yes | — | stable id; **must equal the file stem** |
| `name` | yes | — | device name |
| `mgmt_host` | yes | — | management IP or hostname |
| `mgmt_port` | no | (core default) | omit to let the core inventory service fill its default (22); range 1–65535 |
| `platform` | no | (core default) | omit to let the core inventory service fill its default |
| `attributes` | no | `{}` | Inventory facts, e.g. `vendor`, `model`, and `current_version` |
| `site` | no | — | full derived site path, for example `emea/nl/ams01` |
| `role` | no | — | |
| `tags` | no | `[]` | list of strings |
| `access_config` | no | `{}` | `ssh` / `enable` secret **references** (see below) |
| `custom_fields` | no | `{}` | free-form metadata |

`access_config` carries secret *references*, never raw credentials:

```yaml
access_config:
  ssh:
    username_ref: "{{ secret('vault://devices/ssh-username') }}"
    password_ref: "{{ secret('vault://devices/ssh-password') }}"
    private_key_ref: "{{ secret('vault://devices/ssh-key') }}"
  enable:
    password_ref: "{{ secret('vault://devices/enable-password') }}"
```

## Walkthrough

1. Add your inventory repository under **Settings → Git Repositories** (URL, branch,
   credentials).
2. Put the [`examples/`](examples/) tree (or your own) into that repo, e.g. under
   `inventory/`.
3. Create a `git` inventory provider: pick the repository, set `branch`, and set `path` to
   `inventory`.
4. Run a sync. Devices and sites appear in the inventory; missing platforms get the core
   default.

## Supported object types

`device`, `site`.

## Adding an object type

Object types are extended in code (edit + release the wheel). The Git provider reads YAML,
so a new type means parsing additional files/fields from the repo and mapping them with the
SDK helper:

```python
from hegemony_inventory_sdk import ObjectFieldMapping, map_records

_VLAN = ObjectFieldMapping(
    object_type="vlan",
    identity="external_id",
    name_field="name",
    field_map={"vid": "vid", "description": "description"},
)
# In list_objects("vlan", ...): load the records (e.g. from a vlans/ directory),
# then `return map_records(records, _VLAN, provider_id=self.id)`.
```

Register the type with `register_object_type(ObjectTypeSpec(...))` and add its id to
`supported_resources`. After installing the new wheel version and restarting, it appears in
the inventory submenu with schema-driven list/detail pages — no core change, no migration.
