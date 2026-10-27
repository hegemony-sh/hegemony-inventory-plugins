<!--
SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing

This repository is part of the Hegemony project. Its source is licensed under
`AGPL-3.0-or-later`. Your contribution is licensed under the same terms, and
contributing it transfers no copyright to the project. There is no Contributor
License Agreement.

The project's own files name Rexonix s.r.o. as the copyright holder, and those
with an SPDX header also name Jakub Trávník as the author (see
[LICENSING.md](LICENSING.md)). If you do not write on behalf of Rexonix s.r.o.,
you keep the copyright in your contribution and may add your own
`SPDX-FileCopyrightText` line to the header of a file you change, as REUSE
recommends.

Every commit must be signed off under the
[Developer Certificate of Origin](https://developercertificate.org/) (DCO),
version 1.1. A `Signed-off-by:` line certifies that you wrote the change, or
otherwise have the right to submit it under the project licence. Sign off with
`git commit -s`. The DCO GitHub App checks every commit in a pull request: the
sign-off's name and email must match the commit's author. Merge
commits and commits by GitHub bot accounts are skipped. To add a missing
sign-off to every commit on your branch, run
`git rebase --signoff origin/main` and force-push the branch.

A commit written by an AI coding agent is signed off by the person who directs
the agent and submits the work, as its author; the agent is credited in a
`Co-authored-by:` trailer and never signs off in its own name. See the
platform's
[CONTRIBUTING.md](https://github.com/hegemony-sh/hegemony/blob/develop/CONTRIBUTING.md#licensing-and-dco).

## Code of conduct

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). This adapted third-party document retains
CC-BY-4.0; preserve its attribution and license when editing it. The code
licensing and contribution rules above continue to apply to code.

## Reporting security issues

Do not open a public issue for a vulnerability, a suspected vulnerability or a
leaked secret. Report it privately, as [SECURITY.md](SECURITY.md) describes.

## Development

The [README](README.md#development) sets up the workspace and the pre-commit
hooks, and [Local checkout layout](README.md#local-checkout-layout) places this
repository beside the platform. [Task](https://taskfile.dev/) is optional; these
are the commands of the `Lint, Test, Build Wheels` CI job, in this order:

```bash
uv sync --frozen --all-packages
uv run ruff check packages plugins tests scripts
uv run ruff format --check packages plugins tests scripts
uv run reuse lint
uv run ty check
uv run python scripts/generate_catalogue.py --check
uv run pytest
uv run python scripts/build_wheels.py
uv run python scripts/smoke_install_wheels.py
```

A new file carries an SPDX copyright and licence header, copied from an existing
file, so that `reuse lint` passes; see [LICENSING.md](LICENSING.md). Test fixtures
use example hosts (`netbox.example`, `192.0.2.0/24`, RFC 1918 addresses) and
synthetic tokens, never real hosts, addresses or credentials.

A first-party file with an SPDX header names Rexonix s.r.o. as the copyright
holder and Jakub Trávník as the author, in the file's own comment syntax:

<!-- REUSE-IgnoreStart -->

```python
# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
```

<!-- REUSE-IgnoreEnd -->

## Commit messages and pull requests

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/),
checked by commitlint (`commitlint.config.mjs`) on every pull request. A scope is
optional and free-form: lower-case letters, digits and single dashes, at most 20
characters, comma-separated without spaces (`netbox`, `sdk,docs`).

Open pull requests against `main`, one change each. A merged `feat`, `fix`, `perf`
or `revert` commit releases a new version (see [Releasing](#releasing) below), so
write its subject for the changelog.

## Releasing

A merge to `main` is a release. The `Release` workflow calls the shared
[`plugin-release.yml`](https://github.com/hegemony-sh/hegemony-github-workflows)
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
only for public repositories, so a release cut while this repository was private
has no attestation, only `SHA256SUMS`. That file is published in the same
release as the wheels, so it checks integrity but not provenance.

One failure a re-run cannot repair: semantic-release pushes the release
commit and the `vX.Y.Z` tag before it publishes the GitHub Release, so if
publication fails after that push the tag exists without a Release, and the
next run reads the tag as the last release. Publish that tag's assets by hand
from a checkout of the tag, as the shared workflow's
[README](https://github.com/hegemony-sh/hegemony-github-workflows#recovering-a-tag-without-a-release)
describes (`gh release create vX.Y.Z --verify-tag ... dist/*.whl
dist/SHA256SUMS` after rebuilding the wheels at that tag).

The shared workflow is pinned by commit in `.github/workflows/release.yml`;
a change merged to it reaches this repository through a pull request that
bumps that SHA.
