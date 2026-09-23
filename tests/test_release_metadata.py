# SPDX-FileCopyrightText: 2025-2026 Jakub Travnik <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import hegemony_inventory_sdk as sdk
from hegemony_inventory_sdk import ObjectTypeSpec

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PYPROJECTS = [
    ROOT / "packages" / "inventory_sdk" / "pyproject.toml",
    ROOT / "plugins" / "inventory_netbox" / "pyproject.toml",
    ROOT / "plugins" / "inventory_git" / "pyproject.toml",
]
PROVIDER_PYPROJECTS = PACKAGE_PYPROJECTS[1:]


class RecordingRegistry:
    api_version = sdk.SDK_ABI_VERSION

    def __init__(self) -> None:
        self.providers: dict[str, dict] = {}
        self.object_types: dict[str, ObjectTypeSpec] = {}

    def register_provider_type(self, **kwargs) -> None:
        self.providers[kwargs["provider_type"]] = kwargs

    def register_object_type(self, spec: ObjectTypeSpec) -> None:
        self.object_types[spec.id] = spec


def _metadata(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]


def test_all_packages_share_one_release_version_and_provider_sdk_pin() -> None:
    metadata = [_metadata(path) for path in PACKAGE_PYPROJECTS]
    versions = {item["version"] for item in metadata}

    assert versions == {sdk.__version__}
    for item in [_metadata(path) for path in PROVIDER_PYPROJECTS]:
        assert f"hegemony-inventory-sdk=={sdk.__version__}" in item["dependencies"]


def test_sdk_import_is_dependency_light() -> None:
    code = (
        "import sys, hegemony_inventory_sdk\n"
        "bad = [m for m in ('fastapi', 'sqlalchemy', 'apps', 'temporalio') if m in sys.modules]\n"
        "assert not bad, bad\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_inventory_plugin_entry_points_are_declared() -> None:
    entries = entry_points(group="hegemony.inventory_plugins")
    by_name = {entry.name: entry for entry in entries}

    assert {"netbox", "git"} <= set(by_name)
    registry = RecordingRegistry()
    for name in ("netbox", "git"):
        by_name[name].load()(registry)

    assert set(registry.providers) == {"netbox", "git"}
    assert {"ip_prefix", "ip_address", "vlan"} <= set(registry.object_types)
