<!--
SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
SPDX-FileContributor: Jakub Travnik <jakub.travnik@gmail.com>
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hegemony Inventory Plugins

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![CI](https://github.com/hegemony-sh/hegemony-inventory-plugins/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/hegemony-sh/hegemony-inventory-plugins/actions/workflows/ci.yml)
[![REUSE status](https://api.reuse.software/badge/github.com/hegemony-sh/hegemony-inventory-plugins)](https://api.reuse.software/info/github.com/hegemony-sh/hegemony-inventory-plugins)
[![Latest release](https://img.shields.io/github/v/release/hegemony-sh/hegemony-inventory-plugins?label=Release)](CHANGELOG.md)
[![FOSSA license scan](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fhegemony-sh%2Fhegemony-inventory-plugins.svg?type=shield&issueType=license)](https://app.fossa.com/projects/git%2Bgithub.com%2Fhegemony-sh%2Fhegemony-inventory-plugins?ref=badge_shield&issueType=license)
[![FOSSA security scan](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fhegemony-sh%2Fhegemony-inventory-plugins.svg?type=shield&issueType=security)](https://app.fossa.com/projects/git%2Bgithub.com%2Fhegemony-sh%2Fhegemony-inventory-plugins?ref=badge_shield&issueType=security)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/hegemony-sh/hegemony-inventory-plugins/badge)](https://scorecard.dev/viewer/?uri=github.com/hegemony-sh/hegemony-inventory-plugins)

Inventory SDK and provider plugin wheels for
[Hegemony](https://github.com/hegemony-sh/hegemony), the self-hosted,
[Temporal](https://temporal.io/)-based orchestration platform for long-running
infrastructure changes.

An inventory provider connects Hegemony to an external source of truth and
brings its devices and sites into the inventory, next to what you create by hand
(the built-in `local` source). Providers are optional plugins, so the provider
types an instance offers depend on which wheels are installed. This repository
holds the SDK that providers are written against and the providers Hegemony
ships as opt-in wheels:

| Package | Directory | What it is |
| --- | --- | --- |
| `hegemony-inventory-sdk` | [`packages/inventory_sdk`](packages/inventory_sdk) | The public, dependency-light SDK (pydantic only): the provider base class, value objects, error envelopes, the object-type declaration API and the registration protocol. Hegemony's images include it. |
| `hegemony-inventory-netbox` | [`plugins/inventory_netbox`](plugins/inventory_netbox) | Reads devices, sites and other object families from a NetBox instance over its REST API. |
| `hegemony-inventory-git` | [`plugins/inventory_git`](plugins/inventory_git) | Reads devices and sites from YAML files in a Git repository you already manage. |

The SDK and all provider wheels are released together under one semver tag such
as `v0.4.1`, cut automatically when a releasable change merges to `main`.
Provider wheels depend on the exact SDK version from the same release. The
packages are `0.x`: while the major version is 0, a breaking change releases a
new minor version (see [Contributing](CONTRIBUTING.md#releasing)).

## How this repository relates to Hegemony

- **The platform is a separate repository.**
  [`hegemony-sh/hegemony`](https://github.com/hegemony-sh/hegemony) is the
  application: API, worker, scheduler and UI. It does not contain the SDK or the
  providers; this repository is their source of truth.
- **Hegemony images carry the SDK only.** Provider wheels are installed on top,
  into each API container that should serve that provider type (see
  [Install From A Release](#install-from-a-release)).
- **The platform pins a release of this repository.** Its
  `.github/plugin-pins.json` records the release tag it is built and tested
  against and the commit that tag resolves to. A Hegemony release requires that
  pin to be this repository's release tag `v<SDK version>`.
- **The platform builds from a sibling checkout during development.** See
  [Local checkout layout](#local-checkout-layout).

## Documentation

- The documentation site, [hegemony.sh](https://hegemony.sh), covers the
  platform.
- [Inventory Providers](https://github.com/hegemony-sh/hegemony/blob/develop/docs/guides/inventory-providers.md)
  in the platform documentation is the operator's guide: adding a provider,
  testing the connection, discovery and syncs.
- [Inventory Plugin Development](https://github.com/hegemony-sh/hegemony/blob/develop/docs/development/inventory-plugins.md)
  in the platform documentation describes the sibling layout, the pinned
  revisions and the Docker install model that this README also describes.
- Each package has its own README: [SDK](packages/inventory_sdk/README.md),
  [NetBox](plugins/inventory_netbox/README.md) and
  [Git](plugins/inventory_git/README.md).

## Install From A Release

Provider wheels are opt-in. Hegemony images already include the matching
`hegemony-inventory-sdk` in `/opt/venv`, while provider wheels are installed only in
deployments that need them. Install the release that the Hegemony version you run
pins in its `.github/plugin-pins.json`.

The API process discovers provider entry points and runs inventory syncs, so install
providers into each API container that should serve those providers. Restart the API after
installing so the registry reloads entry points. Do not use `--system`; Hegemony runs from
`/opt/venv`.

Released wheels are published with a `SHA256SUMS` file. Verify downloaded wheels before
installing them. The example installs all providers; remove any wheel names for providers
that deployment should not enable.

```bash
VERSION=0.4.1   # a release tag without the leading v, from the Releases page
API_CONTAINER=<your API container name>

docker exec -u root -it "${API_CONTAINER}" bash -lc "
set -euo pipefail
version=${VERSION}
base=https://github.com/hegemony-sh/hegemony-inventory-plugins/releases/download/v\${version}
tmp=\$(mktemp -d)
cd \"\${tmp}\"
curl -fsSLO \"\${base}/SHA256SUMS\"
for wheel in \
  hegemony_inventory_netbox-\${version}-py3-none-any.whl \
  hegemony_inventory_git-\${version}-py3-none-any.whl
do
  curl -fsSLO \"\${base}/\${wheel}\"
  grep \"  \${wheel}$\" SHA256SUMS | sha256sum -c -
done
uv pip install --python /opt/venv/bin/python --no-deps ./*.whl
rm -rf \"\${tmp}\"
"

docker restart "${API_CONTAINER}"
```

For development, use the same release flow against the dev API container:

```bash
VERSION=0.4.1
API_CONTAINER=hegemony-dev-api-1
# then run the same docker exec install block above
```

Or build local wheels from this repository and copy them into the running dev API
container (run this from the Hegemony checkout, with this repository beside it as
described in [Local checkout layout](#local-checkout-layout)):

```bash
cd ../hegemony-inventory-plugins
task build

API_CONTAINER=hegemony-dev-api-1
docker exec -u root "${API_CONTAINER}" mkdir -p /tmp/inventory-wheels
docker cp dist/. "${API_CONTAINER}:/tmp/inventory-wheels/"
docker exec -u root -it "${API_CONTAINER}" bash -lc '
uv pip install --python /opt/venv/bin/python --no-deps \
  /tmp/inventory-wheels/hegemony_inventory_sdk-*.whl \
  /tmp/inventory-wheels/hegemony_inventory_netbox-*.whl \
  /tmp/inventory-wheels/hegemony_inventory_git-*.whl
'
docker restart "${API_CONTAINER}"
```

These Docker-command installs are runtime changes. Re-run them after recreating the API
container or replacing the image.

## Develop a provider

A provider is an ordinary Python wheel that depends only on
`hegemony-inventory-sdk` and registers itself under the
`hegemony.inventory_plugins` entry-point group. The SDK never imports
Hegemony internals, so a provider can be developed and tested without the
platform.

1. Depend on the SDK and declare the entry point in your `pyproject.toml`:

   ```toml
   [project]
   dependencies = ["hegemony-inventory-sdk>=0.4.1,<0.5"]

   [project.entry-points."hegemony.inventory_plugins"]
   my_plugin = "my_plugin:register"
   ```

2. Subclass `InventoryProvider` and expose
   `register(registry: InventoryPluginRegistry)`, which registers the provider
   type, its configuration model and the object types it can read. The
   [SDK README](packages/inventory_sdk/README.md) shows the shape and explains
   discovery, for providers whose object types depend on the instance they talk
   to.
3. Use the two providers here as references.
   [`plugins/inventory_git`](plugins/inventory_git) is the smaller one;
   [`plugins/inventory_netbox`](plugins/inventory_netbox) shows a remote API,
   secret references and schema-driven object types. Their tests, under
   [`tests/api`](tests/api), use the fakes in
   [`tests/inventory_fakes.py`](tests/inventory_fakes.py), and credentials in
   tests are synthetic.
4. Build the wheel, install it into a development API container as shown in
   [Install From A Release](#install-from-a-release) and restart the API. The
   provider type then appears under **Settings → Inventory Providers**.

Keep Hegemony integration and parity tests in the platform repository, and
provider unit tests and release smoke tests here.

## Local checkout layout

Clone this repository and the platform as siblings for a full development
checkout, and whenever you work on inventory providers or run platform checks
that install provider packages:

```bash
mkdir -p ~/work/hegemony
cd ~/work/hegemony
git clone https://github.com/hegemony-sh/hegemony.git Hegemony
git clone https://github.com/hegemony-sh/hegemony-inventory-plugins.git
```

```text
hegemony/
  Hegemony/
  hegemony-inventory-plugins/
```

During local development Hegemony resolves the inventory packages from the
sibling checkout:

```text
../hegemony-inventory-plugins/packages/inventory_sdk
../hegemony-inventory-plugins/plugins/inventory_netbox
../hegemony-inventory-plugins/plugins/inventory_git
```

Its source-built images receive this repository as the Docker named build context
`inventory_plugins` and copy only `packages/inventory_sdk` from it, so the SDK is
in the base API, worker and scheduler images and the providers are not.

Keep this checkout at the commit pinned in the platform's
`.github/plugin-pins.json` when you run the platform's release-train checks
(`task release:manifest:check`): they stand down or fail on a checkout that is
somewhere else, because the versions it reports would not be the pinned ones.
Make provider implementation changes here.

## Development

```bash
uv sync --all-packages
uv run pre-commit install --install-hooks
```

If you have [Task](https://taskfile.dev/) installed, the common workflow is:

```bash
task setup
task lint
task test
task build
task smoke
```

The hook set mirrors Hegemony where applicable for this package-only repo:
general file hygiene, pyproject validation, typos, Zizmor, workflow schema checks,
REUSE, Ruff, typecheck, tests, Gitleaks, and commitlint. UI, Docker, OpenAPI, and
task-runner hooks stay in Hegemony because those surfaces are not present here.

Run the same checks locally:

```bash
task ci
```

Run every configured pre-commit hook manually:

```bash
task precommit
```

Releases are cut automatically when a releasable change merges to `main`; see
[Contributing](CONTRIBUTING.md#releasing). Do not bump versions or push tags by
hand. `task version:set -- X.Y.Z` and `task version:verify -- vX.Y.Z` stay
available for trying the lockstep bump locally.

## Contributing, security and licence

- Every commit needs a DCO sign-off (`git commit -s`). See
  [Contributing](CONTRIBUTING.md).
- Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Report vulnerabilities privately, as described in
  [Security](SECURITY.md); do not open a public issue for them.
- Licensed under `AGPL-3.0-or-later`. See [Licensing](LICENSING.md).
