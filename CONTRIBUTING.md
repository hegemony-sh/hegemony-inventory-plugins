<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Travnik <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing

This repository is part of the Hegemony project. Public source is licensed under
`AGPL-3.0-or-later`, and Hegemony may also offer separate commercial licenses.

External contributions are accepted only under the
[Hegemony Contributor License Agreement](CLA.md). The CLA must grant Hegemony the
rights needed to distribute accepted contributions under both the public AGPL
license and separate commercial terms.

The CLA check is expected to run in pull requests. Until that automation is in
place, maintainers should not merge external contributions unless the contributor
has already signed the [Hegemony CLA](CLA.md).

## Releasing

A merge to `main` is a release. The `Release` workflow calls the shared
[`plugin-release.yml`](https://github.com/hegemony-sh/hegemony-workflows)
workflow, which runs [semantic-release](https://semantic-release.gitbook.io/):
it reads the Conventional Commits since the last tag, derives the next
version from them, bumps every package and SDK pin (`scripts/set_version.py`),
refreshes `uv.lock`, builds and smoke-installs the wheels, prepends
`CHANGELOG.md`, commits `chore(release): X.Y.Z [skip ci]`, tags `vX.Y.Z` and
publishes the wheels plus `SHA256SUMS` as the GitHub Release. There is no
manual step any more: do not bump versions or push `v*` tags by hand.

| Commits since the last tag | Release |
|---|---|
| `fix`, `perf`, `revert` | patch |
| `feat` | minor |
| breaking change (`feat!:`, `fix!:` or a `BREAKING CHANGE:` footer) | **minor, while the major is 0** |
| `chore`, `docs`, `ci`, `test`, `refactor`, `build`, `style` | none |

The 0.x rule is the `"breaking": true` entry in `.releaserc.json`'s
`releaseRules`; once the packages reach 1.0, change its `release` to
`major`. Deleting it is not enough: the rules are tried in order and the
first match wins, so a breaking `feat` would still match the `feat` rule and
release a minor.

Releases are immutable. The pipeline refuses to run when a GitHub Release for
the next tag already exists and never replaces published assets: fix forward
with a new commit, which becomes the next release. Attestations are created
only for public repositories, so releases cut while this repository is
private ship checksums only.

One failure a re-run cannot repair: semantic-release pushes the release
commit and the `vX.Y.Z` tag before it publishes the GitHub Release, so if
publication fails after that push the tag exists without a Release, and the
next run reads the tag as the last release. Publish that tag's assets by hand
from a checkout of the tag, as the shared workflow's
[README](https://github.com/hegemony-sh/hegemony-workflows#recovering-a-tag-without-a-release)
describes (`gh release create vX.Y.Z --verify-tag ... dist/*.whl
dist/SHA256SUMS` after rebuilding the wheels at that tag).

The shared workflow is pinned by commit in `.github/workflows/release.yml`;
a change merged to it reaches this repository through a pull request that
bumps that SHA.
