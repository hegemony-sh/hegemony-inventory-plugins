# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Travnik <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Set the unified release version across all workspace packages."""

from __future__ import annotations

import argparse
import re

from package_paths import PACKAGE_DIRS, PROVIDER_DIRS, SDK_VERSION_FILE

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]+)?$")


def _replace(pattern: str, replacement: str, text: str, *, path: str) -> str:
    next_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Could not update {path}: pattern not found")
    return next_text


def _set_project_version(pyproject, version: str) -> None:
    text = pyproject.read_text(encoding="utf-8")
    text = _replace(
        r'^version = "[^"]+"$',
        f'version = "{version}"',
        text,
        path=str(pyproject),
    )
    pyproject.write_text(text, encoding="utf-8")


def _set_provider_sdk_pin(pyproject, version: str) -> None:
    text = pyproject.read_text(encoding="utf-8")
    text = re.sub(
        r"hegemony-inventory-sdk(?:==|>=)[^\",<\]]*(?:,<0\.2)?",
        f"hegemony-inventory-sdk=={version}",
        text,
        count=1,
    )
    pyproject.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="Release version without a leading v, e.g. 0.1.0")
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"Invalid semver release version: {args.version}")

    for package_dir in PACKAGE_DIRS:
        _set_project_version(package_dir / "pyproject.toml", version)
    for provider_dir in PROVIDER_DIRS:
        _set_provider_sdk_pin(provider_dir / "pyproject.toml", version)

    text = SDK_VERSION_FILE.read_text(encoding="utf-8")
    text = _replace(
        r'^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
        text,
        path=str(SDK_VERSION_FILE),
    )
    SDK_VERSION_FILE.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
