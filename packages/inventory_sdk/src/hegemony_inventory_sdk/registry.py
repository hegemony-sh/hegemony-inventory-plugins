# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The registry facade contract a plugin's ``register(registry)`` callable receives.

The core platform supplies a concrete object satisfying this Protocol. Plugins program
against the Protocol only, never against the platform's registry internals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from .objecttype import ObjectTypeSpec
from .provider import InventoryProvider

ProviderFactory = Callable[..., InventoryProvider]


@runtime_checkable
class InventoryPluginRegistry(Protocol):
    """Registration surface passed to ``register(registry)`` plugin callables."""

    #: The platform's plugin registration ABI version (see ``SDK_ABI_VERSION``).
    api_version: int

    def register_provider_type(
        self,
        *,
        provider_type: str,
        display_name: str,
        description: str,
        capabilities: list[str],
        supported_resources: list[str],
        factory: ProviderFactory,
        config_model: type[BaseModel] | None = None,
        config_schema: dict[str, Any] | None = None,
        default_config: dict[str, Any] | None = None,
    ) -> None:
        """Register an inventory provider type."""

    def register_object_type(self, spec: ObjectTypeSpec) -> None:
        """Register an inventory object type."""
