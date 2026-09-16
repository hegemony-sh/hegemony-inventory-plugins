# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Infrahub inventory provider plugin."""

from typing import Any

from hegemony_inventory_sdk import (
    SDK_ABI_VERSION,
    Capability,
    InventoryPluginRegistry,
    PlatformServices,
    ResourceType,
)

from .config import InfrahubProviderConfig
from .provider import InfrahubInventoryProvider


def build_infrahub_provider(
    *, provider_id: str, config: dict[str, Any], services: PlatformServices
) -> InfrahubInventoryProvider:
    return InfrahubInventoryProvider(provider_id=provider_id, config=config, services=services)


def register(registry: InventoryPluginRegistry) -> None:
    """Entry point for the ``hegemony.inventory_plugins`` group."""
    if registry.api_version != SDK_ABI_VERSION:
        raise RuntimeError(
            f"Infrahub inventory plugin requires SDK ABI {SDK_ABI_VERSION}; "
            f"platform provides {registry.api_version}"
        )
    registry.register_provider_type(
        provider_type="infrahub",
        display_name="Infrahub",
        description="Branch-aware GraphQL inventory from Infrahub",
        capabilities=[Capability.READ.value, Capability.QUERY.value],
        supported_resources=[ResourceType.DEVICE.value, ResourceType.SITE.value],
        factory=build_infrahub_provider,
        config_model=InfrahubProviderConfig,
        default_config={
            "verify_tls": True,
            "branch": "main",
            "device_kind": "InfraDevice",
            "timeout_seconds": 10.0,
        },
    )


__all__ = ["InfrahubInventoryProvider", "register"]
