# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider plugin."""

from typing import Any

from hegemony_inventory_sdk import (
    Capability,
    InventoryPluginRegistry,
    PlatformServices,
    ResourceType,
)

from .config import NetBoxProviderConfig
from .object_types import ALL_OBJECT_TYPE_IDS, object_type_specs
from .provider import NetBoxInventoryProvider


def build_netbox_provider(
    *, provider_id: str, config: dict[str, Any], services: PlatformServices
) -> NetBoxInventoryProvider:
    return NetBoxInventoryProvider(provider_id=provider_id, config=config, services=services)


def register(registry: InventoryPluginRegistry) -> None:
    """Entry point for the ``hegemony.inventory_plugins`` group.

    Every family in ``object_types.OBJECT_TYPES`` is registered, which is what makes it
    selectable and renderable, and every one is declared as a supported resource, which
    is what lets the platform's own object-type selection reach it. That selection is
    the only say in what a provider reads: this plugin deliberately keeps no setting of
    its own for the same question.
    """
    for spec in object_type_specs():
        registry.register_object_type(spec)
    registry.register_provider_type(
        provider_type="netbox",
        display_name="NetBox",
        description=(
            "Read and query devices, sites and any of NetBox's object families -- "
            "IPAM, interfaces, racks, circuits, virtual machines and more"
        ),
        capabilities=[Capability.READ.value, Capability.QUERY.value],
        supported_resources=[
            ResourceType.DEVICE.value,
            ResourceType.SITE.value,
            *ALL_OBJECT_TYPE_IDS,
        ],
        factory=build_netbox_provider,
        config_model=NetBoxProviderConfig,
        default_config={
            "verify_tls": True,
            "query_cache_ttl_seconds": None,
            "timeout_seconds": 10.0,
        },
    )


__all__ = ["NetBoxInventoryProvider", "register"]
