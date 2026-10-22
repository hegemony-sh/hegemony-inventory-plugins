# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Travnik <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify a release tag matches every package version and provider SDK pin."""

from __future__ import annotations

import argparse
import os
import re
import tomllib

from package_paths import PACKAGE_DIRS, PROVIDER_DIRS, SDK_VERSION_FILE


def _project_metadata(pyproject) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "tag",
        nargs="?",
        default=os.environ.get("GITHUB_REF_NAME", ""),
        help="Release tag, usually vX.Y.Z",
    )
    args = parser.parse_args()
    version = args.tag.removeprefix("v")
    if not version:
        raise SystemExit("Release tag is required")

    failures: list[str] = []

    for package_dir in PACKAGE_DIRS:
        metadata = _project_metadata(package_dir / "pyproject.toml")
        if metadata["version"] != version:
            failures.append(
                f"{metadata['name']} version is {metadata['version']}, expected {version}"
            )

    expected_sdk_dependency = f"hegemony-inventory-sdk=={version}"
    for provider_dir in PROVIDER_DIRS:
        metadata = _project_metadata(provider_dir / "pyproject.toml")
        if expected_sdk_dependency not in metadata["dependencies"]:
            failures.append(f"{metadata['name']} must depend on {expected_sdk_dependency}")

    sdk_version_text = SDK_VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', sdk_version_text, re.MULTILINE)
    if match is None or match.group(1) != version:
        failures.append(f"{SDK_VERSION_FILE} __version__ must be {version}")

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
