# SPDX-FileCopyrightText: 2025-2026 Jakub Travnik <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PACKAGE_DIRS = (
    ROOT / "packages" / "inventory_sdk",
    ROOT / "plugins" / "inventory_netbox",
    ROOT / "plugins" / "inventory_git",
)

PROVIDER_DIRS = PACKAGE_DIRS[1:]
SDK_VERSION_FILE = (
    ROOT / "packages" / "inventory_sdk" / "src" / "hegemony_inventory_sdk" / "_version.py"
)
