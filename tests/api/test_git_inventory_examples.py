# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The committed Git-inventory example parses through the loader.

Keeps ``plugins/inventory_git/examples/`` a living, validated artifact: if the inventory
schema or loader drifts, the documented example (and the README that points at it) breaks
loudly here rather than silently misleading users.
"""

from __future__ import annotations

from pathlib import Path

from hegemony_inventory_git.loader import load_git_inventory

_GIT_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "inventory_git"


def test_example_inventory_loads_cleanly() -> None:
    result = load_git_inventory(_GIT_PLUGIN_ROOT, "examples")

    assert result.errors == []
    assert sorted(d.external_id for d in result.devices) == ["core-switch-1", "edge-router-1"]
    # Hierarchical site external ids are derived from the directory layout.
    assert sorted(result.sites) == ["emea", "emea/nl", "emea/nl/ams01"]
    assert result.sites["emea/nl"].parent_external_id == "emea"
    assert result.sites["emea/nl/ams01"].parent_external_id == "emea/nl"


def test_example_demonstrates_core_platform_default() -> None:
    result = load_git_inventory(_GIT_PLUGIN_ROOT, "examples")
    platforms = {d.external_id: d.platform for d in result.devices}

    # edge-router sets a platform explicitly; core-switch omits it so the core
    # inventory service fills the default during materialization.
    assert platforms["edge-router-1"] == "ios-xe"
    assert platforms["core-switch-1"] is None
