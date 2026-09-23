# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trim a NetBox ``/api/schema/`` document down to a test fixture.

A stock NetBox 4.3 serves about 3 MiB of OpenAPI, most of it prose and validation
detail that :mod:`hegemony_inventory_netbox.derive` never reads. This keeps the
collection endpoints, the component schemas they reach, and nothing else, so the
fixture stays reviewable and the deriver still sees a real document rather than one
written to make it pass.

Usage::

    python scripts/trim_netbox_schema.py schema.json tests/fixtures/netbox-4.3-schema.json

Fetch the input from a running NetBox with::

    curl -H 'Accept: application/json' https://netbox.example/api/schema/ > schema.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

LIST_ENDPOINT = re.compile(r"^/api/(?:plugins/[a-z0-9_-]+/)?[a-z0-9-]+/[a-z0-9-]+/$")

#: Keys carrying prose or validation detail the deriver ignores. Dropped only where a
#: schema node uses them as keywords -- never where they are the *name* of a property,
#: which is why ``properties`` is walked by hand below. ``description`` and ``title``
#: are both real NetBox field names, so a blanket key filter silently deletes them.
DROP_KEYWORDS = frozenset(
    {
        "default",
        "description",
        "example",
        "examples",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "title",
        "x-spec-enum-id",
    }
)


def strip_node(node: Any) -> Any:
    """Drop ignored keywords from a schema node, keeping property names intact."""
    if isinstance(node, dict):
        kept: dict[str, Any] = {}
        for key, value in node.items():
            if key in DROP_KEYWORDS:
                continue
            if key == "properties" and isinstance(value, dict):
                kept[key] = {name: strip_node(sub) for name, sub in value.items()}
            else:
                kept[key] = strip_node(value)
        return kept
    if isinstance(node, list):
        return [strip_node(item) for item in node]
    return node


def component_refs(node: Any, found: set[str]) -> None:
    """Every ``#/components/schemas/X`` reference reachable from ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            component_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            component_refs(item, found)


def trim(schema: dict[str, Any]) -> dict[str, Any]:
    """The collection endpoints of ``schema`` and the components they reach."""
    components = schema.get("components", {}).get("schemas", {})
    paths: dict[str, Any] = {}
    wanted: set[str] = set()
    for path, operations in schema.get("paths", {}).items():
        if not LIST_ENDPOINT.match(path) or "get" not in operations:
            continue
        try:
            body = operations["get"]["responses"]["200"]["content"]["application/json"]
        except (KeyError, TypeError):
            continue
        paths[path] = {"get": {"responses": {"200": {"content": {"application/json": body}}}}}
        component_refs(body, wanted)

    resolved: set[str] = set()
    while wanted - resolved:
        name = (wanted - resolved).pop()
        resolved.add(name)
        if name in components:
            component_refs(components[name], wanted)

    return {
        "openapi": schema["openapi"],
        "info": {"title": schema["info"]["title"], "version": schema["info"]["version"]},
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": {
                name: strip_node(components[name])
                for name in sorted(resolved)
                if name in components
            }
        },
    }


def main(argv: list[str]) -> int:
    """Trim ``argv[1]`` into ``argv[2]``."""
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, destination = Path(argv[1]), Path(argv[2])
    trimmed = trim(json.loads(source.read_text()))
    destination.write_text(json.dumps(trimmed, indent=1) + "\n")
    print(
        f"{destination}: {len(trimmed['paths'])} endpoints, "
        f"{len(trimmed['components']['schemas'])} components, "
        f"{destination.stat().st_size / 1024:.0f} KiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
