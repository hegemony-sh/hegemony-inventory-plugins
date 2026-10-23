<!--
SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
SPDX-FileContributor: Jakub Travnik <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Licensing

The source code in this repository is licensed under `AGPL-3.0-or-later`,
and under no other licence. See [LICENSE](LICENSE) and
[LICENSES/AGPL-3.0-or-later.txt](LICENSES/AGPL-3.0-or-later.txt).

Rexonix s.r.o. holds the copyright in the project's own files, and Jakub
Trávník is their author. Each first-party file that carries an SPDX header says
so in REUSE form: `SPDX-FileCopyrightText` names Rexonix s.r.o. and
`SPDX-FileContributor` names Jakub Trávník. Files without a header (`uv.lock`,
the recorded NetBox schemas `tests/fixtures/*-schema.json`, the JSON
configuration files `.releaserc.json` and `.github/renovate.json`, and
`CHANGELOG.md`, which semantic-release rewrites) are annotated in `REUSE.toml`
instead, which names the copyright holder but not the author.

Contributions are accepted under the same licence, with a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off
on every commit; there is no Contributor License Agreement. A contributor who
does not write on behalf of Rexonix s.r.o. keeps the copyright in their
contribution. See [CONTRIBUTING.md](CONTRIBUTING.md).

Keep first-party source code SPDX identifiers as
(preserve third-party notices):

```text
SPDX-License-Identifier: AGPL-3.0-or-later
```

## Code of Conduct

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) adapts Contributor Covenant 2.1 by
Coraline Ada Ehmke and its contributors. That document retains **CC-BY-4.0**,
with upstream attribution and local changes recorded in the file. Its license
text is in [LICENSES/CC-BY-4.0.txt](LICENSES/CC-BY-4.0.txt). This documentation
exception does not change the licenses of the project's code or plugin wheels.
