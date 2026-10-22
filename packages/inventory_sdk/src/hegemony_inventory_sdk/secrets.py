# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Secret-reference types.

Providers pass opaque reference strings through; the platform resolves them via
``PlatformServices.resolve_secret_ref`` (see :mod:`hegemony_inventory_sdk.services`).
Resolved secret values never appear in provider config, descriptors, or logs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# An opaque secret reference string (e.g. a protected Jinja ``{{ secret('...') }}``
# expression). Providers pass these through; they must never embed resolved values.
SecretRef = str


@runtime_checkable
class SecretResolver(Protocol):
    """Resolves an opaque secret reference to its value, platform-side."""

    async def __call__(self, ref: SecretRef, *, operation: str) -> str | None: ...
