<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Security Policy

Security reports are handled privately. Please do not open a public GitHub issue for vulnerabilities, suspected vulnerabilities, leaked secrets, or exploit details.

## Scope

This repository holds the Hegemony inventory SDK (`hegemony-inventory-sdk`) and the NetBox and Git inventory provider wheels (`hegemony-inventory-netbox`, `hegemony-inventory-git`). Report here a vulnerability in one of these packages: for example how a provider handles credentials or tokens, builds requests to the system it reads, or parses the data it receives.

A vulnerability in the Hegemony platform itself belongs to the platform's [security policy](https://github.com/hegemony-sh/hegemony/blob/develop/SECURITY.md). Both are reported to the same address.

## Supported versions

The three packages are versioned in lockstep and released from `main` (see [CONTRIBUTING.md](CONTRIBUTING.md#releasing)). This repository does not maintain older release lines: a security fix is published as a new release, and releases are immutable, so a published release is never changed.

Hegemony pins the release of this repository it is built against. When a fix is released here, the platform moves its pin to the new release tag, as the platform's [release process](https://github.com/hegemony-sh/hegemony/blob/develop/docs/development/release-process.md) describes. The platform's [supported versions](https://github.com/hegemony-sh/hegemony/blob/develop/SECURITY.md#supported-versions) say which platform lines receive security fixes.

If you install the wheels into your own Hegemony deployment, update to the newest release when a security advisory names your version.

## Reporting a vulnerability

Report vulnerabilities privately by emailing [security@hegemony.sh](mailto:security@hegemony.sh).

Please include as much detail as you can safely share:

- Affected package (`hegemony-inventory-sdk`, `hegemony-inventory-netbox` or `hegemony-inventory-git`).
- Impact and attack scenario.
- Reproduction steps or proof-of-concept details.
- Affected versions or commits, the provider configuration, and the version of the system the provider reads (for example the NetBox release).
- Whether the issue is already public or under active exploitation.
- A safe way to contact you for follow-up.

Do not include production secrets (NetBox API tokens, device credentials, Git credentials), customer data, private keys, or exploit traffic from systems you do not own or have permission to test.

## Response targets

Maintainers aim to follow this timeline:

| Stage | Target |
| --- | --- |
| Acknowledge receipt | 48 hours |
| Initial triage and severity assessment | 7 days |
| Coordinated fix and disclosure target | 90 days |

These targets may vary based on severity, complexity, affected dependencies, maintainer availability, and responsible-disclosure coordination.

## Disclosure process

1. Maintainers acknowledge the report and establish a private coordination channel.
2. Maintainers validate the report, assess severity, and identify affected versions.
3. A fix, mitigation, or advisory is prepared privately.
4. Disclosure timing is coordinated with the reporter when possible.
5. Public details are published after a fix or mitigation is available, or after the coordinated disclosure window expires.

Each vulnerability is published as an advisory on this repository's [Security tab](https://github.com/hegemony-sh/hegemony-inventory-plugins/security/advisories), with the affected and fixed versions, and the fix ships as a new release. To be notified, watch this repository and choose **Watch → Custom → Releases**.

## Safe harbor

Security research conducted in good faith is welcome. Please avoid privacy violations, service disruption, data destruction, persistence, social engineering, spam, and testing against systems you do not own or do not have permission to test.

## Secrets

If you believe a secret was committed or exposed, report it privately. Do not rotate, use, or test credentials unless they belong to you and you are authorized to do so.
