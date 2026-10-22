# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Combine NetBox filters without widening independently configured restrictions."""

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
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

# How NetBox combines repeated values of ONE parameter, which is what decides how two
# independently configured restrictions on that parameter can be ANDed. Verified against
# NetBox's ``utilities/constants.py`` (the ``FILTER_*_LOOKUP_MAP`` tables) and its
# ``MultiValue*`` filters, which extend django-filter's ``MultipleChoiceFilter``: repeated
# values are ORed into one ``Q``, and a lookup whose name starts with ``n`` flips the
# filter to ``queryset.exclude()``, so every listed value is excluded.
#
#   family    parameters                       repeated values      conjunction of two
#   --------  -------------------------------  -------------------  ---------------------
#   ANY       no suffix, ie, empty, tag__any   OR (value IN list)   intersect the sets
#   NONE      n, nic, nisw, niew, nie          all excluded (AND)   union the sets
#   PATTERN   ic, isw, iew, regex, iregex      OR of the matches    subset only, else refuse
#   BOUND     gt, gte, lt, lte                 OR, loosest wins     keep the tighter bound
#   ALL       tag, tag_id (no suffix)          AND                  union the sets
#
# ANY intersects because its atoms exclude each other (a status is one value), so what
# both sides allow is exactly the common values. PATTERN atoms overlap -- a name can
# contain "core" and "edge" -- so the only conjunction NetBox can express as repeated
# parameters is one side being a subset of the other; anything else would have to be
# widened to an OR, and is refused instead. BOUND spellings are distinct parameters:
# ``id__gt=5`` beside ``id__gte=5`` is already their conjunction, because NetBox ANDs
# different parameters, so strict and inclusive bounds are never folded into one.
_NONE_SUFFIXES: frozenset[str] = frozenset({"n", "nic", "nisw", "niew", "nie"})
_PATTERN_SUFFIXES: frozenset[str] = frozenset({"ic", "isw", "iew", "regex", "iregex"})
_LOWER_BOUND_SUFFIXES: frozenset[str] = frozenset({"gt", "gte"})
_UPPER_BOUND_SUFFIXES: frozenset[str] = frozenset({"lt", "lte"})
_ALL_PARAMETERS: frozenset[str] = frozenset({"tag", "tag_id"})


def _split(parameter: str) -> tuple[str, str]:
    """``status__n`` -> ``("status", "n")``; a parameter with no lookup has suffix ``""``."""
    field, separator, suffix = parameter.rpartition("__")
    return (field, suffix) if separator else (parameter, "")


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _bound_key(parameter: str, value: str) -> Decimal | datetime:
    """A comparable form of a bound: NetBox bounds are numbers or ISO 8601 stamps."""
    try:
        return Decimal(value)
    except InvalidOperation:
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"NetBox {parameter} bounds cannot be compared: a value is neither a number "
            "nor an ISO 8601 date"
        ) from None


def _tighter_bound(parameter: str, suffix: str, existing: list[str], incoming: list[str]) -> str:
    """The one value that says what both bound lists together say.

    Within one query the values are ORed, so each side is as loose as its loosest value;
    what both require is the tighter of those two.
    """
    keys = {value: _bound_key(parameter, value) for value in (*existing, *incoming)}
    kinds = {type(key) for key in keys.values()}
    if len(kinds) > 1:
        raise ValueError(f"NetBox {parameter} bounds cannot be compared: mixed number and date")
    loosest: Callable[..., str]
    tighter: Callable[..., str]
    if suffix in _LOWER_BOUND_SUFFIXES:
        loosest, tighter = min, max
    else:
        loosest, tighter = max, min
    try:
        return tighter(
            (loosest(existing, key=keys.__getitem__), loosest(incoming, key=keys.__getitem__)),
            key=keys.__getitem__,
        )
    except TypeError:
        # A naive and an aware datetime; Python refuses to order them, and so do we.
        raise ValueError(
            f"NetBox {parameter} bounds cannot be compared: mixed naive and zoned stamps"
        ) from None


def _conjoin(parameter: str, existing: list[str], incoming: list[str]) -> list[str]:
    """Values of ``parameter`` that require both ``existing`` and ``incoming``."""
    field, suffix = _split(parameter)
    if suffix in _NONE_SUFFIXES or (not suffix and field in _ALL_PARAMETERS):
        return _unique([*existing, *incoming])
    if suffix in _LOWER_BOUND_SUFFIXES or suffix in _UPPER_BOUND_SUFFIXES:
        return [_tighter_bound(parameter, suffix, existing, incoming)]
    if suffix in _PATTERN_SUFFIXES:
        for narrower, wider in ((incoming, existing), (existing, incoming)):
            if set(narrower) <= set(wider):
                return _unique(narrower)
        raise ValueError(
            f"NetBox cannot require two independent {parameter} restrictions in one "
            "request: repeated values of a pattern lookup match any of them"
        )
    common = [value for value in existing if value in incoming]
    if not common:
        raise ValueError(f"Conflicting NetBox scope constraints for {parameter}")
    return _unique(common)


def intersect_queries(*queries: str) -> str:
    """Combine restrictions so that every query's restriction still holds.

    Repeated values within one raw query retain NetBox's native meaning. Between
    independently configured restrictions, each parameter is combined the way its lookup
    family allows (see the table above), and a conjunction NetBox cannot express is an
    error rather than a wider read.
    """
    combined: dict[str, list[str]] = {}
    for query in queries:
        current: dict[str, list[str]] = {}
        for key, value in parse_qsl(query.lstrip("?"), keep_blank_values=True):
            current.setdefault(key, []).append(value)
        for key, values in current.items():
            if key not in combined:
                combined[key] = values
            else:
                combined[key] = _conjoin(key, combined[key], values)
    return urlencode([(key, value) for key, values in combined.items() for value in values])
