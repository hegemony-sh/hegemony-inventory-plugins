# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Inventory provider value objects (framework-free dataclasses).

``ObjectDescriptor`` is the generic wire shape for any object type. ``DeviceDescriptor``
and ``SiteRef`` are the device/site specializations the current pipeline consumes; they
remain first-class and are bridged onto the generic store by the platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from .enums import MaintenanceState, ResourceType

AccessConfig = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderCallContext:
    """Per-call metadata.

    Platform capabilities (secret resolution, URL/SSRF validation, Git transport, scale
    limits) are injected at provider construction via ``PlatformServices``, not here.
    """

    provider_id: str
    operation: str
    run_id: UUID | None = None
    step_id: str | None = None
    selector_index: int | None = None
    query_hash: str | None = None
    config_version: int | None = None
    source_version: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObjectRelationship:
    """A directed relationship from one object to another (by external id)."""

    rel: str
    target_object_type: str
    target_external_id: str


@dataclass(frozen=True, slots=True)
class ObjectDescriptor:
    """Generic provider object for any registered object type."""

    provider_id: str
    object_type: str
    external_id: str | None
    name: str
    display_name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    descriptive: dict[str, Any] = field(default_factory=dict)
    access_config: AccessConfig = field(default_factory=dict)
    relationships: tuple[ObjectRelationship, ...] = ()
    source_version: dict[str, Any] = field(default_factory=dict)

    @property
    def safe_display_name(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True, slots=True)
class SiteRef:
    provider_id: str
    external_id: str
    name: str
    parent_external_id: str | None = None
    location: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    descriptive: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    provider_id: str
    resource_type: ResourceType
    external_id: str
    display_name: str
    descriptive: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    provider_id: str
    external_id: str | None
    name: str
    mgmt_host: str
    # Optional: providers emit the source platform or leave it unset; the core
    # inventory service fills the default (DEFAULT_DEVICE_PLATFORM) on materialization.
    platform: str | None = None
    display_name: str | None = None
    hostname: str | None = None
    # Optional: providers emit the source management port or leave it unset; the core
    # inventory service fills the default (DEFAULT_DEVICE_MGMT_PORT) on materialization.
    mgmt_port: int | None = None
    vendor: str | None = None
    model: str | None = None
    current_version: str | None = None
    site: SiteRef | None = None
    role: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    native_tags: tuple[str, ...] = ()
    access_config: AccessConfig = field(default_factory=dict)
    descriptive: dict[str, Any] = field(default_factory=dict)
    source_version: dict[str, Any] = field(default_factory=dict)
    # Optional: the source system's lifecycle state for this device, normalized to
    # ``MaintenanceState``. Providers emit it when the source models one (a NetBox
    # device status, say) and leave it unset otherwise; the core inventory service
    # keeps the row's existing state when it is ``None``.
    maintenance_state: MaintenanceState | None = None

    @property
    def safe_display_name(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    provider_id: str
    resource_type: ResourceType
    external_id: str
    change_type: str
    occurred_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderTestResult:
    ok: bool
    provider_id: str
    provider_type: str
    latency_ms: int | None = None
    message: str | None = None
    error: dict[str, Any] | None = None
    safe_details: dict[str, Any] = field(default_factory=dict)


# ── Descriptor → generic ObjectDescriptor adapters ──


def device_to_object_descriptor(device: DeviceDescriptor) -> ObjectDescriptor:
    """Adapt a device-specific descriptor to the generic object shape."""
    relationships: tuple[ObjectRelationship, ...] = ()
    if device.site is not None:
        relationships = (
            ObjectRelationship(
                rel="site",
                target_object_type=ResourceType.SITE.value,
                target_external_id=device.site.external_id,
            ),
        )
    attributes: dict[str, Any] = {
        "mgmt_host": device.mgmt_host,
        "mgmt_port": device.mgmt_port,
        "platform": device.platform,
        "hostname": device.hostname,
        "vendor": device.vendor,
        "model": device.model,
        "current_version": device.current_version,
        "role": device.role,
        "tags": dict(device.tags),
        "native_tags": list(device.native_tags),
        "maintenance_state": device.maintenance_state.value
        if device.maintenance_state is not None
        else None,
    }
    return ObjectDescriptor(
        provider_id=device.provider_id,
        object_type=ResourceType.DEVICE.value,
        external_id=device.external_id,
        name=device.name,
        display_name=device.display_name,
        attributes=attributes,
        descriptive=dict(device.descriptive),
        access_config=dict(device.access_config),
        relationships=relationships,
        source_version=dict(device.source_version),
    )


def site_to_object_descriptor(site: SiteRef) -> ObjectDescriptor:
    """Adapt a site reference to the generic object shape."""
    relationships: tuple[ObjectRelationship, ...] = ()
    if site.parent_external_id:
        relationships = (
            ObjectRelationship(
                rel="parent",
                target_object_type=ResourceType.SITE.value,
                target_external_id=site.parent_external_id,
            ),
        )
    return ObjectDescriptor(
        provider_id=site.provider_id,
        object_type=ResourceType.SITE.value,
        external_id=site.external_id,
        name=site.name,
        display_name=site.name,
        attributes={"location": site.location, "tags": dict(site.tags)},
        descriptive=dict(site.descriptive),
        relationships=relationships,
    )
