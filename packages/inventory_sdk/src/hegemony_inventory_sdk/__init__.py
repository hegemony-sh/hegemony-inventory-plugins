# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public SDK for Hegemony inventory plugins (providers + object types).

Dependency-light (pydantic only). Out-of-tree plugin wheels depend on this package and
never import Hegemony app internals.
"""

from __future__ import annotations

from ._version import SDK_ABI_VERSION, __version__
from .access import ACCESS_CONFIG_JSON_SCHEMA, AccessConfigModel
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
from .discovery import TRANSFORMABLE_FIELDS, DiscoveryResult, ProjectionBinding
from .enums import Capability, MaintenanceState, ProviderErrorCode, ResourceType
from .errors import InventoryProviderError, ProviderErrorEnvelope, not_supported
from .identifiers import (
    OBJECT_TYPE_ID_MAX_LENGTH,
    OBJECT_TYPE_ID_PATTERN,
    validate_object_type_id,
)
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
from .transforms import TRANSFORMS, apply_transforms, normalize_transforms

__all__ = [
    "normalize_transforms",
    "apply_transforms",
    "TRANSFORMS",
    "TRANSFORMABLE_FIELDS",
    "DEFAULT_DEVICE_MGMT_PORT",
    "DEFAULT_DEVICE_PLATFORM",
    "SDK_ABI_VERSION",
    "ACCESS_CONFIG_JSON_SCHEMA",
    "AccessConfig",
    "AccessConfigModel",
    "Capability",
    "ChangeEvent",
    "DeviceDescriptor",
    "DiscoveryResult",
    "ProjectionBinding",
    "GitFetcher",
    "GitWorkdir",
    "InventoryLimits",
    "InventoryPluginRegistry",
    "InventoryProvider",
    "InventoryProviderError",
    "MaintenanceState",
    "ObjectDescriptor",
    "OBJECT_TYPE_ID_MAX_LENGTH",
    "OBJECT_TYPE_ID_PATTERN",
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
    "validate_object_type_id",
]
