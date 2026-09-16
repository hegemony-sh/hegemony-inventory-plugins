#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate the shipped NetBox catalogue from a recorded schema.

The catalogue used to be written by hand: fifty families, three hundred attributes and
fifty edges, each typed out and each able to disagree with the NetBox it describes. Six
defects found in review came from exactly that gap, and the deriver exists because a
NetBox states all of it itself, correctly, for the release in front of you.

So the shipped catalogue is now generated from a recorded schema by the same deriver
that reads a live one. Two things follow, and both are the point:

* it cannot drift from the deriver's own behaviour, because it *is* the deriver's
  output -- a class of bug that no amount of review on a hand-written table removes;
* what stays hand-written is only :mod:`~hegemony_inventory_netbox.overlay`, the
  claims a schema does not make.

Run with no arguments to rewrite the generated module in place::

    uv run python scripts/generate_catalogue.py

``--check`` instead reports whether the checked-in file is what this script would write,
which is what CI runs: a deriver or overlay change that nobody regenerated fails there
rather than shipping a catalogue that no longer matches the code that made it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "plugins" / "inventory_netbox" / "src" / "hegemony_inventory_netbox"
SCHEMA = REPO / "tests" / "fixtures" / "netbox-4.3-schema.json"
TARGET = PACKAGE / "catalogue_generated.py"
README = REPO / "plugins" / "inventory_netbox" / "README.md"

#: The README block this script owns. Everything between the markers is regenerated.
README_BEGIN = "<!-- begin generated catalogue -->"
README_END = "<!-- end generated catalogue -->"

sys.path.insert(0, str(PACKAGE.parent))

from collections.abc import Mapping  # noqa: E402

from hegemony_inventory_netbox.catalogue_model import (  # noqa: E402
    Attr,
    NetBoxObjectType,
)
from hegemony_inventory_netbox.derive import (  # noqa: E402
    DerivedCatalogue,
    derive_catalogue,
    registered_catalogue,
)
from hegemony_inventory_sdk import ProjectionBinding  # noqa: E402

#: ``Attr`` shorthand per JSON type, matching the helpers in ``catalogue_model``. An
#: attribute that constrains its values cannot use one and is written out in full.
_HELPERS = {
    "string": "_text",
    "integer": "_int",
    "number": "_number",
    "boolean": "_bool",
    "array": "_list",
    "object": "_json",
}

HEADER = '''# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The NetBox families this provider can read. **Generated -- do not edit.**

Written by ``scripts/generate_catalogue.py`` from a recorded NetBox {version} schema,
through the same deriver that reads a live instance. Change the deriver or the overlay
and regenerate; editing this file by hand puts it beyond the reach of both.

{families} families, {attributes} attributes, {relationships} relationships.

This is a *seed*, not the last word. A provider that has discovered its own instance
uses what that instance said it serves; this is what it offers until then, and what it
falls back to when discovery has never succeeded.
"""

from __future__ import annotations

from hegemony_inventory_sdk import ProjectionBinding

from .catalogue_model import (
    Attr,
    NetBoxObjectType,
    _bool,
    _int,
    _json,
    _list,
    _number,
    _rel,
    _text,
)

OBJECT_TYPES: tuple[NetBoxObjectType, ...] = (
'''


def _literal(value: object) -> str:
    """A Python literal for a value the deriver produced."""
    return repr(value)


def _attribute(attr: Attr) -> str:
    """One attribute, as the shortest call that reproduces it exactly."""
    if attr.enum:
        parts = [_literal(attr.path), _literal(attr.json_type)]
        if not attr.nullable:
            parts.append("nullable=False")
        parts.append(f"enum={_literal(attr.enum)}")
        return f"Attr({', '.join(parts)})"
    helper = _HELPERS[attr.json_type]
    if attr.json_type in {"array", "object"}:
        # Neither helper takes ``nullable``; the deriver always leaves these nullable.
        return f"{helper}({_literal(attr.path)})"
    if attr.nullable:
        return f"{helper}({_literal(attr.path)})"
    return f"{helper}({_literal(attr.path)}, nullable=False)"


_BINDING_FIELDS: tuple[str, ...] = (
    "target",
    "object_type",
    "name",
    "hostname",
    "device_hostname",
    "vendor",
    "model",
    "platform",
    "serial",
    "location",
    "site_edge",
    "parent_edge",
    "maintenance_state",
    "maintenance_state_map",
    "secret_refs",
    "default_access_config",
    "tags",
    "descriptive",
    "tag_list",
    "tag_prefix",
    "external_id_prefix",
    "transforms",
)


def _binding(binding: ProjectionBinding) -> str:
    """A projection binding, with only the fields it actually sets."""
    empty = ProjectionBinding(target=binding.target, object_type=binding.object_type)
    parts = [
        f"{name}={_literal(dict(value) if isinstance(value, Mapping) else value)}"
        for name in _BINDING_FIELDS
        if (value := getattr(binding, name)) != getattr(empty, name)
        or name in ("target", "object_type")
    ]
    return f"ProjectionBinding({', '.join(parts)})"


def _family(entry: NetBoxObjectType) -> str:
    """One family as a ``NetBoxObjectType(...)`` call, defaults omitted."""
    lines = [
        "    NetBoxObjectType(",
        f"        id={_literal(entry.id)},",
        f"        endpoint={_literal(entry.endpoint)},",
        f"        app={_literal(entry.app)},",
        f"        display_name={_literal(entry.display_name)},",
        f"        plural={_literal(entry.plural)},",
        f"        description={_literal(entry.description)},",
    ]
    if entry.name_field != "display":
        lines.append(f"        name_field={_literal(entry.name_field)},")
    if entry.display_name_field != "display":
        lines.append(f"        display_name_field={_literal(entry.display_name_field)},")
    if entry.identity != "id":
        lines.append(f"        identity={_literal(entry.identity)},")
    if entry.identity_keys != ("id",):
        lines.append(f"        identity_keys={_literal(entry.identity_keys)},")
    if entry.columns != ("name",):
        lines.append(f"        columns={_literal(entry.columns)},")
    if entry.search_fields != ("name",):
        lines.append(f"        search_fields={_literal(entry.search_fields)},")
    if entry.icon is not None:
        lines.append(f"        icon={_literal(entry.icon)},")
    if entry.base_query:
        lines.append(f"        base_query={_literal(entry.base_query)},")
    if entry.binding is not None:
        lines.append(f"        binding={_binding(entry.binding)},")
    lines.append("        attributes={")
    for name, attr in entry.attributes.items():
        lines.append(f"            {_literal(name)}: {_attribute(attr)},")
    lines.append("        },")
    if entry.relationships:
        lines.append("        relationships=(")
        for edge in entry.relationships:
            lines.append(
                f"            _rel({_literal(edge.rel)}, "
                f"{_literal(edge.target_object_type)}, {_literal(edge.source)}),"
            )
        lines.append("        ),")
    lines.append("    ),")
    return "\n".join(lines)


def render(derived: DerivedCatalogue, catalogue: tuple[NetBoxObjectType, ...]) -> str:
    """The generated module, exactly as it should appear on disk."""
    body = "\n".join(_family(entry) for entry in catalogue)
    header = HEADER.format(
        version=derived.source_version or "4.3",
        families=len(catalogue),
        attributes=sum(len(entry.attributes) for entry in catalogue),
        relationships=sum(len(entry.relationships) for entry in catalogue),
    )
    return f"{header}{body}\n)\n"


def render_readme_table(catalogue: tuple[NetBoxObjectType, ...]) -> str:
    """The README's family table, one row per NetBox app.

    Generated for the same reason the catalogue is: a table of a hundred ids maintained
    by hand is a promise that drifts into a lie, and the test that guards it can only
    say *that* it drifted, never fix it.
    """
    by_app: dict[str, list[str]] = {}
    for entry in catalogue:
        by_app.setdefault(entry.app, []).append(entry.id)
    rows = "\n".join(
        f"| `{app}` | " + ", ".join(f"`{name}`" for name in sorted(ids)) + " |"
        for app, ids in sorted(by_app.items())
    )
    return (
        f"{README_BEGIN}\n\n"
        f"{len(catalogue)} families across {len(by_app)} NetBox apps. Each family carries its\n"
        "app as its `ObjectTypeSpec.group`, which is how the platform's *Object types to\n"
        "sync* selection shows them as a tree rather than a hundred flat checkboxes.\n\n"
        "| NetBox app | Families |\n| --- | --- |\n"
        f"{rows}\n\n{README_END}"
    )


def _replace_readme_block(text: str, block: str) -> str:
    start = text.index(README_BEGIN)
    end = text.index(README_END) + len(README_END)
    return text[:start] + block + text[end:]


def _formatted(text: str) -> str:
    """``text`` as ``ruff format`` would write it, so the check cannot fail on layout."""
    done = subprocess.run(
        ["uv", "run", "ruff", "format", "--stdin-filename", str(TARGET), "-"],
        input=text,
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    if done.returncode != 0:
        raise SystemExit(f"ruff format failed:\n{done.stderr}")
    return done.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether the checked-in file matches, and write nothing.",
    )
    args = parser.parse_args(argv)

    derived = derive_catalogue(json.loads(SCHEMA.read_text()))
    if derived.warnings:
        # A warning means the deriver met a shape it does not model. Generating past one
        # would bake that gap into the shipped catalogue silently.
        raise SystemExit(
            "the recorded schema derives with warnings; teach the overlay first:\n  "
            + "\n  ".join(derived.warnings)
        )
    catalogue = registered_catalogue(derived)
    wanted = _formatted(render(derived, catalogue))
    readme = _replace_readme_block(README.read_text(), render_readme_table(catalogue))

    if args.check:
        stale = [
            path.relative_to(REPO)
            for path, produced in ((TARGET, wanted), (README, readme))
            if not path.exists() or path.read_text() != produced
        ]
        if not stale:
            print("✓ the shipped catalogue and its README table are what the deriver produces")
            return 0
        print(
            f"✗ stale: {', '.join(str(path) for path in stale)}\n"
            "  Run 'uv run python scripts/generate_catalogue.py' and commit the result.",
            file=sys.stderr,
        )
        return 1

    TARGET.write_text(wanted)
    README.write_text(readme)
    print(f"wrote {TARGET.relative_to(REPO)} ({wanted.count(chr(10)) + 1} lines)")
    print(f"wrote the catalogue table in {README.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
