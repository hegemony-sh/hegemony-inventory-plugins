# SPDX-FileCopyrightText: 2026 Jakub Trávník <jakub.travnik@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Combine NetBox filters without widening independently configured restrictions."""

from urllib.parse import parse_qsl, urlencode

LOOKUP_SUFFIX = {
    "exact": "",
    "not": "__n",
    "contains": "__ic",
    "not_contains": "__nic",
    "starts_with": "__isw",
    "ends_with": "__iew",
    "gt": "__gt",
    "gte": "__gte",
    "lt": "__lt",
    "lte": "__lte",
    "empty": "__empty",
}


def intersect_queries(*queries: str) -> str:
    """Combine restrictions without turning repeated OR filters into a union.

    Repeated values within one raw query retain NetBox's native meaning. Between
    independently configured restrictions they must intersect. Tags are NetBox's
    explicit AND filters and can safely accumulate values.
    """
    combined: dict[str, list[str]] = {}
    for query in queries:
        current: dict[str, list[str]] = {}
        for key, value in parse_qsl(query.lstrip("?"), keep_blank_values=True):
            current.setdefault(key, []).append(value)
        for key, values in current.items():
            if key not in combined:
                combined[key] = values
            elif key in {"tag", "tag_id"}:
                combined[key] = list(dict.fromkeys([*combined[key], *values]))
            else:
                common = [value for value in combined[key] if value in values]
                if not common:
                    raise ValueError(f"Conflicting NetBox scope constraints for {key}")
                combined[key] = list(dict.fromkeys(common))
    return urlencode([(key, value) for key, values in combined.items() for value in values])
