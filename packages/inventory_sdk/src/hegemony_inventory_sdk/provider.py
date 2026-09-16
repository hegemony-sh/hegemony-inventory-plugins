# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Inventory provider abstract contract.

The device/site methods are the current stable contract. The generic
``list_objects``/``get_object`` methods are the forward path for arbitrary object
types; they default to delegating to the device/site methods so existing providers keep
working unchanged until they opt in.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from .descriptors import (
    AccessConfig,
    ChangeEvent,
    DeviceDescriptor,
    ObjectDescriptor,
    ProviderCallContext,
    ProviderTestResult,
    ResourceRef,
    SiteRef,
    device_to_object_descriptor,
    site_to_object_descriptor,
)
from .discovery import DiscoveryResult, ProjectionBinding
from .enums import Capability, ResourceType
from .errors import not_supported

__all__ = ["AccessConfig", "InventoryProvider"]


def _query_mapping_to_expr(query: Mapping[str, Any] | None) -> str:
    if not query:
        return ""
    return "&".join(f"{key}={value}" for key, value in query.items())


class InventoryProvider(ABC):
    """Base class for inventory providers (built-in and plugin-supplied)."""

    id: str
    provider_type: str
    capabilities: frozenset[Capability]
    supported_resources: frozenset[ResourceType]
    config_schema: Mapping[str, Any]

    @abstractmethod
    async def list(
        self,
        resource_type: ResourceType,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ResourceRef]:
        """List resource references."""

    @abstractmethod
    async def get(
        self,
        resource_type: ResourceType,
        external_id: str,
        *,
        context: ProviderCallContext,
    ) -> DeviceDescriptor | SiteRef | ResourceRef:
        """Get a single resource descriptor."""

    @abstractmethod
    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        """Query device descriptors."""

    @abstractmethod
    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        """List read-only site references."""

    @abstractmethod
    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        """Validate provider connectivity/configuration."""

    # ── Generic object access (forward path; default delegates to device/site) ──

    async def list_objects(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ObjectDescriptor]:
        """List objects of ``object_type`` as generic descriptors.

        Providers that only implement the device/site contract inherit this default,
        which adapts ``query_devices``/``list_sites`` output. Providers serving custom
        object types override this method.
        """
        if object_type == ResourceType.DEVICE.value:
            devices = await self.query_devices(
                _query_mapping_to_expr(query), limit=limit, context=context
            )
            return [device_to_object_descriptor(device) for device in devices]
        if object_type == ResourceType.SITE.value:
            sites = await self.list_sites(limit=limit, context=context)
            return [site_to_object_descriptor(site) for site in sites]
        raise not_supported(
            getattr(self, "id", "unknown"),
            getattr(self, "provider_type", "unknown"),
            f"list_objects:{object_type}",
        )

    async def get_object(
        self,
        object_type: str,
        external_id: str,
        *,
        context: ProviderCallContext,
    ) -> ObjectDescriptor:
        """Get one object of ``object_type`` as a generic descriptor."""
        if object_type in (ResourceType.DEVICE.value, ResourceType.SITE.value):
            result = await self.get(ResourceType(object_type), external_id, context=context)
            if isinstance(result, DeviceDescriptor):
                return device_to_object_descriptor(result)
            if isinstance(result, SiteRef):
                return site_to_object_descriptor(result)
            return ObjectDescriptor(
                provider_id=result.provider_id,
                object_type=object_type,
                external_id=result.external_id,
                name=result.display_name,
                display_name=result.display_name,
                descriptive=dict(result.descriptive),
            )
        raise not_supported(
            getattr(self, "id", "unknown"),
            getattr(self, "provider_type", "unknown"),
            f"get_object:{object_type}",
        )

    # ── Optional capabilities ──

    async def discover(self, *, context: ProviderCallContext) -> DiscoveryResult:
        """Describe the object types *this instance* serves.

        A provider type's object types are declared once, at registration, and are the
        same for every instance of it. That is right for a source of truth with a fixed
        shape and wrong for one that can be extended: a NetBox with plugins installed
        serves models this plugin has never heard of, and every instance has its own
        custom fields.

        A provider declaring :attr:`~.enums.Capability.DISCOVER` implements this to let
        the platform ask. The default raises ``not_supported``, so a provider whose shape
        really is fixed needs no change and behaves exactly as before.

        Expensive by nature -- it describes a whole API rather than reading one object --
        so the platform calls it on demand and stores the answer, rather than per sync.
        """
        raise not_supported(self.id, self.provider_type, "discover")

    def configured_projection_bindings(
        self, object_types: Iterable[str]
    ) -> tuple[ProjectionBinding, ...] | None:
        """Rebuild discovered projection bindings from current mutable configuration.

        Discovery still decides which object families this instance serves. Override
        this when ordinary provider settings influence how those families project at
        sync time. Returning None keeps the bindings stored during discovery.
        """
        return None

    # ── Optional future capabilities ──

    async def update(
        self,
        resource_type: ResourceType,
        external_id: str,
        patch: Mapping[str, Any],
        *,
        context: ProviderCallContext,
    ) -> None:
        raise not_supported(self.id, self.provider_type, "update")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> builtins.list[ChangeEvent]:
        raise not_supported(self.id, self.provider_type, "parse_webhook")
