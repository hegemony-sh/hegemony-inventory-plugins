# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public SDK for Hegemony inventory plugins (providers + object types).

Dependency-light (pydantic only). Out-of-tree plugin wheels depend on this package and
never import Hegemony app internals.
"""

from __future__ import annotations

from ._version import SDK_ABI_VERSION, __version__
from .constants import DEFAULT_DEVICE_MGMT_PORT, DEFAULT_DEVICE_PLATFORM
from .descriptors import (
    AccessConfig,
    ChangeEvent,
    DeviceDescriptor,
    ObjectDescriptor,
    ObjectRelationship,
    ProviderCallContext,
    ProviderTestResult,
    ResourceRef,
    SiteRef,
    device_to_object_descriptor,
    site_to_object_descriptor,
)
from .discovery import DiscoveryResult
from .enums import Capability, MaintenanceState, ProviderErrorCode, ResourceType
from .errors import InventoryProviderError, ProviderErrorEnvelope, not_supported
from .mapping import (
    ObjectFieldMapping,
    RelationshipMapping,
    extract_path,
    map_record,
    map_records,
)
from .objecttype import (
    ObjectTypeRelationshipSpec,
    ObjectTypeSpec,
    ObjectTypeUIHints,
)
from .provider import InventoryProvider
from .registry import InventoryPluginRegistry, ProviderFactory
from .secrets import SecretRef, SecretResolver
from .services import GitFetcher, GitWorkdir, InventoryLimits, PlatformServices

__all__ = [
    "DEFAULT_DEVICE_MGMT_PORT",
    "DEFAULT_DEVICE_PLATFORM",
    "SDK_ABI_VERSION",
    "AccessConfig",
    "Capability",
    "ChangeEvent",
    "DeviceDescriptor",
    "DiscoveryResult",
    "GitFetcher",
    "GitWorkdir",
    "InventoryLimits",
    "InventoryPluginRegistry",
    "InventoryProvider",
    "InventoryProviderError",
    "MaintenanceState",
    "ObjectDescriptor",
    "ObjectFieldMapping",
    "ObjectRelationship",
    "ObjectTypeRelationshipSpec",
    "ObjectTypeSpec",
    "ObjectTypeUIHints",
    "PlatformServices",
    "RelationshipMapping",
    "ProviderCallContext",
    "ProviderErrorCode",
    "ProviderErrorEnvelope",
    "ProviderFactory",
    "ProviderTestResult",
    "ResourceRef",
    "ResourceType",
    "SecretRef",
    "SecretResolver",
    "SiteRef",
    "__version__",
    "device_to_object_descriptor",
    "extract_path",
    "map_record",
    "map_records",
    "not_supported",
    "site_to_object_descriptor",
]
