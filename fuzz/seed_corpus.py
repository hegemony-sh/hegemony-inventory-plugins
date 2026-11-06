# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate deterministic raw UTF-8 seed inputs; no external data or credentials."""

import hashlib
import sys
from pathlib import Path

SOURCES = {
    "netbox_filters_fuzzer": "hegemony_inventory_netbox.filters",
    "object_mapping_fuzzer": "hegemony_inventory_sdk.mapping,hegemony_inventory_sdk.identifiers",
}

SEEDS = {
    "netbox_filters_fuzzer": [
        "status=1&status=2\nstatus=2",
        "tag=a\ntag=b",
        "id__gt=1\nid__gt=NaN",
        "id__lt=Infinity\nid__lt=5",
        "id__gt=sNaN\nid__gt=1",
        "id__gte=2026-01-01\nid__gte=2026-02-01",
        "id__gt=2026-01-01\nid__gt=2026-01-01T00:00:00%2B00:00",
        "name__ic=core\nname__ic=edge",
        "status=a%26b\nstatus=a%26b",
    ],
    "object_mapping_fuzzer": [
        'device\na.b\n{"a":{"b":42}}',
        "invalid/id\nmissing\n{}",
        "" + "a" * 100 + '\na\n{"a":null}',
        "" + "a" * 101 + "\na\n{}",
        "device\na\n" + "[" * 1100 + "0" + "]" * 1100 + "",
    ],
}


def seed_corpus(root: Path) -> None:
    for name, seeds in SEEDS.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        for seed in seeds:
            data = seed.encode("utf-8")
            (directory / hashlib.sha256(data).hexdigest()).write_bytes(data)


if __name__ == "__main__":
    seed_corpus(Path(sys.argv[1]))
