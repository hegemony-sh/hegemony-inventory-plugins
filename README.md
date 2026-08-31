<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Travnik <jakub.travnik@gmail.com>
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# hegemony-inventory-plugins

Standalone release repo for Hegemony inventory plugin packages:

- `hegemony-inventory-sdk`
- `hegemony-inventory-netbox`
- `hegemony-inventory-infrahub`
- `hegemony-inventory-git`

The SDK and all provider wheels are released together from unified semver tags such as
`v0.1.0`. Provider wheels depend on the exact SDK version from the same release.

Public source is licensed under `AGPL-3.0-or-later`; commercial licenses may be
available separately. See [Licensing](LICENSING.md).

Contributions require the [Hegemony Contributor License Agreement](CLA.md). See
[Contributing](CONTRIBUTING.md).

## Install From A Release

Provider wheels are opt-in. Hegemony images already include the matching
`hegemony-inventory-sdk` in `/opt/venv`, while provider wheels are installed only in
deployments that need them.

The API process discovers provider entry points and runs inventory syncs, so install
providers into each API container that should serve those providers. Restart the API after
installing so the registry reloads entry points. Do not use `--system`; Hegemony runs from
`/opt/venv`.

Released wheels are published with a `SHA256SUMS` file. Verify downloaded wheels before
installing them. The example installs all providers; remove any wheel names for providers
that deployment should not enable.

```bash
VERSION=0.2.1
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
  hegemony_inventory_infrahub-\${version}-py3-none-any.whl \
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
VERSION=0.2.1
API_CONTAINER=hegemony-dev-api-1
# then run the same docker exec install block above
```

Or build local wheels from this repository and copy them into the running dev API
container:

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
  /tmp/inventory-wheels/hegemony_inventory_infrahub-*.whl \
  /tmp/inventory-wheels/hegemony_inventory_git-*.whl
'
docker restart "${API_CONTAINER}"
```

These Docker-command installs are runtime changes. Re-run them after recreating the API
container or replacing the image.

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

Before tagging a release, update every package version and provider SDK pin:

```bash
task version:set -- 0.1.0
task lock
```

Tags must match package metadata. A `v0.1.0` tag publishes four wheels plus
`SHA256SUMS` to the matching GitHub Release.

Releases are intended to be immutable: the release workflow fails if a GitHub
Release for the tag already exists and never replaces published assets. If a
release artifact is wrong, cut a new patch tag instead of mutating the existing
release. The release workflow also
creates GitHub artifact attestations for the wheel files and checksum file,
but only for public releases: attestation needs a public repository or an
Enterprise plan, so releases cut while this repository is private ship
checksums only.
