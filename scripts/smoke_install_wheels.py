# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Travnik <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Install built wheels in a clean virtualenv and verify imports, entry points and licences."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from package_paths import PACKAGE_DIRS, ROOT

EXPECTED_MODULES = (
    "hegemony_inventory_sdk",
    "hegemony_inventory_netbox",
    "hegemony_inventory_git",
)

EXPECTED_ENTRY_POINTS = {"netbox", "git"}


def expected_licenses() -> dict[str, str]:
    """Distribution name -> the SPDX licence expression its pyproject declares."""
    licenses: dict[str, str] = {}
    for package_dir in PACKAGE_DIRS:
        with (package_dir / "pyproject.toml").open("rb") as fh:
            project = tomllib.load(fh)["project"]
        licenses[project["name"]] = project["license"]
    return licenses


def _python_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def main() -> None:
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if len(wheels) != len(EXPECTED_MODULES):
        raise SystemExit(f"Expected {len(EXPECTED_MODULES)} wheels in dist/, found {len(wheels)}")

    tmp = Path(tempfile.mkdtemp(prefix="hegemony-inventory-wheel-smoke-"))
    try:
        venv_dir = tmp / "venv"
        subprocess.run(["uv", "venv", str(venv_dir)], check=True)
        python = _python_bin(venv_dir)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), *map(str, wheels)],
            cwd=ROOT,
            check=True,
        )
        code = f"""
from importlib import import_module
from importlib.metadata import distribution, entry_points

# Every wheel declares its licence and carries the texts it names
# (dist-info/licenses/, from license-files).
for name, expression in {expected_licenses()!r}.items():
    dist = distribution(name)
    assert dist.metadata["License-Expression"] == expression, (
        name, dist.metadata["License-Expression"]
    )
    files = dist.metadata.get_all("License-File") or []
    assert files, (name, "no License-File")
    for file in files:
        text = dist.read_text("licenses/" + file)
        assert text and text.strip(), (name, file)

modules = {EXPECTED_MODULES!r}
for module in modules:
    import_module(module)

entries = entry_points(group="hegemony.inventory_plugins")
names = {{entry.name for entry in entries}}
expected = {EXPECTED_ENTRY_POINTS!r}
missing = expected - names
assert not missing, missing
"""
        subprocess.run([str(python), "-c", code], check=True)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
