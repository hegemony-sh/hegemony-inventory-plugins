# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a configured instance says it serves.

The object types a provider *type* offers are a compile-time constant. The ones an
*instance* serves are not, and the gap is the whole reason this module exists: a NetBox
with plugins installed serves models the plugin was never written to know about, every
instance has its own custom fields, and an older release does not serve everything a
newer one does.

This lives apart from :mod:`.descriptors` because it joins the two halves of a provider
-- the platform-facing :class:`~.objecttype.ObjectTypeSpec` and the provider-facing
:class:`~.mapping.ObjectFieldMapping` -- and ``mapping`` already depends on
``descriptors``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .mapping import ObjectFieldMapping
from .objecttype import ObjectTypeSpec

__all__ = ["DiscoveryResult"]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What one configured instance says it serves.

    The object types a provider *type* offers are a compile-time constant; the ones an
    *instance* serves are not. A NetBox with plugins installed serves models the plugin
    was never written to know about, every NetBox has its own custom fields, and an older
    release does not serve everything a newer one does. Discovery is how the instance
    answers for itself.

    ``object_types`` and ``mappings`` are the two halves of the same answer and are keyed
    alike: the platform renders and stores the spec, and hands the mapping back to the
    provider when it asks for that family. A type present in one and missing from the
    other cannot be read, so both are validated together.
    """

    #: The instance's own version, for cache invalidation: a description is only valid
    #: for the release that produced it.
    source_version: str | None = None
    object_types: tuple[ObjectTypeSpec, ...] = ()
    mappings: Mapping[str, ObjectFieldMapping] = field(default_factory=dict)
    #: Shapes the provider could not interpret. Never fatal -- the family is simply not
    #: offered -- but each one is something this instance serves and the plugin does not
    #: yet understand, which is how a new release or plugin model gets noticed.
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a result whose two halves disagree, before anything stores it."""
        declared = {spec.id for spec in self.object_types}
        mapped = set(self.mappings)
        if declared != mapped:
            missing = sorted(declared - mapped) or sorted(mapped - declared)
            raise ValueError(
                "DiscoveryResult object_types and mappings must describe the same "
                f"object types; these appear in only one: {missing}"
            )
