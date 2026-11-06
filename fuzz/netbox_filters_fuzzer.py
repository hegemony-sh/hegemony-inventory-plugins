#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Raw seeds contain one query per line; generated queries verify conjunction semantics."""

import operator
import sys
from urllib.parse import parse_qs, parse_qsl, urlencode

import atheris

with atheris.instrument_imports(include=["hegemony_inventory_netbox"]):
    from hegemony_inventory_netbox.filters import intersect_queries


@atheris.instrument_func
def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    left = [fdp.ConsumeIntInRange(-20, 20) for _ in range(fdp.ConsumeIntInRange(1, 5))]
    right = [fdp.ConsumeIntInRange(-20, 20) for _ in range(fdp.ConsumeIntInRange(1, 5))]
    # A shared atom ensures the ANY conjunction is expressible, so rejecting
    # everything cannot satisfy this harness.
    right.append(left[0])
    for parameter, expected in (
        ("status", set(left) & set(right)),
        ("status__n", set(left) | set(right)),
        ("tag", set(left) | set(right)),
    ):
        queries = [urlencode([(parameter, n) for n in values]) for values in (left, right)]
        combined = parse_qs(intersect_queries(*queries))
        assert set(combined) == {parameter}
        assert {int(value) for value in combined[parameter]} == expected
    # Independent truth-table oracle: each input is an OR of comparisons;
    # the combined query must mean their AND for every candidate in this domain.
    for suffix, compare in (
        ("gt", operator.gt),
        ("gte", operator.ge),
        ("lt", operator.lt),
        ("lte", operator.le),
    ):
        parameter = "id__" + suffix
        queries = [urlencode([(parameter, n) for n in values]) for values in (left, right)]
        values = [int(v) for v in parse_qs(intersect_queries(*queries))[parameter]]
        for candidate in range(-21, 22):
            expected = any(compare(candidate, n) for n in left) and any(
                compare(candidate, n) for n in right
            )
            assert any(compare(candidate, n) for n in values) == expected
    assert parse_qs(intersect_queries("name__ic=core&name__ic=edge", "name__ic=core")) == {
        "name__ic": ["core"]
    }
    for queries in [("status=1", "status=2"), ("name__ic=core", "name__ic=edge")]:
        try:
            intersect_queries(*queries)
        except ValueError:
            pass
        else:
            raise AssertionError("an unrepresentable conjunction was widened")

    queries = data[:16384].decode("utf-8", errors="replace").split("\n")[:3]
    try:
        combined = intersect_queries(*queries)
    except ValueError:
        return
    kept = {k for k, _ in parse_qsl(combined, keep_blank_values=True)}
    for query in queries:
        assert {k for k, _ in parse_qsl(query.lstrip("?"), keep_blank_values=True)} <= kept


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
