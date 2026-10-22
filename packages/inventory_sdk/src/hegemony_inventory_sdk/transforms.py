# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The named, closed set of value transforms a provider may ask the platform to apply.

A value read out of a source is not always the value the platform wants to store. NetBox
states an interface address as ``10.0.0.1/24``; what connects to a device is
``10.0.0.1``. Both are correct -- the stored object should keep the address as the source
states it, and the device column has to hold something a transport can dial -- so the
difference is not a defect to fix at the source but a conversion to declare.

Until now the platform performed that one conversion itself, in its projection code,
which put a NetBox-shaped fact inside the platform and left every other field with
nowhere to say it needed anything. This module is where such a claim goes instead: the
provider names a transform, the platform applies it, and the knowledge sits with the
provider that has it.

**Why a closed vocabulary rather than an expression.** The obvious design is to let a
provider send a small expression, or a callable, and have the platform evaluate it. Both
mean provider-authored logic running inside the API process, reached through a database
row that survives the plugin that wrote it -- a discovery recorded months ago would
still be executable. A fixed set of names cannot do that. What travels is a string the
platform looks up in this table; a name it does not recognise resolves to nothing at
all.

**A name this table does not know is an error, never a silent skip.** A provider that
asks for a transform the platform cannot perform has stated a requirement, not a
preference, and quietly storing the raw value would produce rows that look right and are
not. The cost is real and deliberate: a provider naming a transform added after the
platform it runs against fails discovery with a message naming the transform, rather
than proceeding on values nobody checked. Adding an entry here is therefore a
compatibility event, and the set is meant to stay small.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

__all__ = ["TRANSFORMS", "apply_transforms", "normalize_transforms", "validate_transforms"]


def _host(value: Any) -> Any:
    """An address without its prefix length: ``10.0.0.1/24`` becomes ``10.0.0.1``.

    Splits on the first ``/`` only, which leaves an IPv6 address intact --
    ``2001:db8::1/64`` becomes ``2001:db8::1`` -- and leaves a bare hostname alone,
    since a hostname never contains one.
    """
    if not isinstance(value, str):
        return value
    return value.split("/", 1)[0]


def _lower(value: Any) -> Any:
    """Lowercase, for a source that spells a slug or a platform name inconsistently."""
    return value.lower() if isinstance(value, str) else value


def _upper(value: Any) -> Any:
    """Uppercase, for the sources that state a serial or an asset tag in either case."""
    return value.upper() if isinstance(value, str) else value


def _strip(value: Any) -> Any:
    """Surrounding whitespace, which a free-text field in any source eventually carries."""
    return value.strip() if isinstance(value, str) else value


def _first(value: Any) -> Any:
    """The first entry of a list, for a single-valued relation a source serves as many.

    A non-list passes through, so declaring this against a field that is sometimes a
    list and sometimes a scalar is safe rather than sometimes wrong. An empty list
    becomes ``None``, which is what "there is no value here" already means everywhere
    else.
    """
    if isinstance(value, str | Mapping):
        return value
    if isinstance(value, Sequence):
        return value[0] if value else None
    return value


#: Every transform the platform will perform, by the name a provider uses to ask for it.
#:
#: Each is total and pure: it returns a value for every input, never raises, and never
#: reads anything outside its argument. A value it has nothing to say about passes
#: through unchanged, so transforms compose in any order without one of them having to
#: know what the others did.
TRANSFORMS: Mapping[str, Callable[[Any], Any]] = {
    "host": _host,
    "lower": _lower,
    "upper": _upper,
    "strip": _strip,
    "first": _first,
}


def validate_transforms(names: Iterable[str], *, where: str) -> None:
    """Raise :class:`ValueError` naming the first transform this platform cannot perform.

    ``where`` names the field being validated, because the useful half of the message is
    which declaration to fix, not which name was wrong.
    """
    for name in names:
        if name not in TRANSFORMS:
            known = ", ".join(sorted(TRANSFORMS))
            raise ValueError(
                f"{where}: unknown transform {name!r}; this platform performs: {known}"
            )


def normalize_transforms(
    value: Mapping[str, Sequence[str]], *, where: str
) -> dict[str, tuple[str, ...]]:
    """Validate a declaration and return every chain as a tuple.

    Needed even where an author already wrote tuples, because a binding round-trips
    through JSON between the discovery that produced it and every sync that reads it,
    and JSON has only lists. Without this a restored binding would compare unequal to
    the one that was stored.

    A bare string is refused rather than accepted as a one-item chain. It is the obvious
    thing to write, and ``str`` is itself a ``Sequence[str]``, so accepting the shape
    silently would iterate the name's characters -- ``"host"`` becoming four transforms
    called ``h``, ``o``, ``s``, ``t``. Refusing it names the fix instead.
    """
    normalized: dict[str, tuple[str, ...]] = {}
    for key, names in value.items():
        if isinstance(names, str):
            raise ValueError(
                f"{where}.{key}: transforms must be a list, not the bare string "
                f"{names!r} -- write [{names!r}]"
            )
        chain = tuple(names)
        validate_transforms(chain, where=f"{where}.{key}")
        normalized[key] = chain
    return normalized


def apply_transforms(value: Any, names: Sequence[str]) -> Any:
    """Apply ``names`` to ``value`` in order.

    An unknown name cannot reach here -- both declaration sites validate -- so this
    raises :class:`KeyError` rather than skipping, on the principle that a transform
    that did not happen must never look like one that did.
    """
    for name in names:
        value = TRANSFORMS[name](value)
    return value
