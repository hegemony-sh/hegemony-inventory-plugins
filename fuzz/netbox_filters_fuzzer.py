#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fuzz the NetBox filter conjunction that keeps every configured restriction.

The queries come from provider configuration and from the caller. They either
combine into one query that keeps every parameter they name, or the
combination is refused with ValueError; never another exception, and never a
dropped parameter, which would widen what the provider reads.
"""

import sys
from urllib.parse import parse_qsl

import atheris

with atheris.instrument_imports():
    from hegemony_inventory_netbox.filters import intersect_queries


def _keys(query: str) -> set[str]:
    return {key for key, _ in parse_qsl(query.lstrip("?"), keep_blank_values=True)}


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    queries = [
        fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 256))
        for _ in range(fdp.ConsumeIntInRange(1, 3))
    ]
    try:
        combined = intersect_queries(*queries)
    except ValueError:
        return
    kept = _keys(combined)
    for query in queries:
        assert _keys(query) <= kept, (queries, combined)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
