# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Minimal Git inventory query DSL."""

from __future__ import annotations

from fnmatch import fnmatch
from urllib.parse import parse_qs

from .inventory_schema import GitInventoryDeviceV1
from .loader import LoadedGitSite


def device_tags(device: GitInventoryDeviceV1) -> list[str]:
    seen: dict[str, None] = {}
    for tag in device.tags:
        seen.setdefault(tag, None)
    return list(seen.keys())


def matches_query(device: GitInventoryDeviceV1, sites: dict[str, LoadedGitSite], expr: str) -> bool:
    parsed = parse_qs(expr.lstrip("?"), keep_blank_values=False)
    required_tags = parsed.get("tags", []) + parsed.get("tag", [])
    if required_tags:
        tags = set(device_tags(device))
        for raw in required_tags:
            for tag in raw.split(","):
                if tag.strip() and tag.strip() not in tags:
                    return False
    site_values = parsed.get("site", [])
    if site_values:
        site = sites.get(device.site) if device.site else None
        if site is None:
            return False
        wanted = {value.strip() for raw in site_values for value in raw.split(",") if value.strip()}
        if site.external_id not in wanted and site.model.name not in wanted:
            return False
    names = [
        pattern.strip()
        for raw in parsed.get("name", [])
        for pattern in raw.split(",")
        if pattern.strip()
    ]
    return not (names and not any(fnmatch(device.name, pattern) for pattern in names))
