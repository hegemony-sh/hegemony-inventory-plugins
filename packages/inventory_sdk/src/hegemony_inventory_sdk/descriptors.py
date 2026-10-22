# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Inventory provider value objects (framework-free dataclasses).

``ObjectDescriptor`` is the generic wire shape for any object type. ``DeviceDescriptor``
and ``SiteRef`` are the device/site specializations the current pipeline consumes; they
remain first-class and are bridged onto the generic store by the platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .enums import MaintenanceState, ResourceType

if TYPE_CHECKING:  # pragma: no cover - import cycle: mapping.py reads this module
    from .mapping import ObjectFieldMapping

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
    #: How to read the family being asked for, when the platform knows and the provider
    #: might not.
    #:
    #: An object type a provider *discovered* is not in the catalogue it shipped -- that
    #: is the whole point of discovering it -- so a later call cannot look up how to map
    #: it. A provider is built fresh for every operation and cannot remember either. The
    #: platform stored the mapping discovery returned, and hands it back here.
    #:
    #: ``None`` means the provider is on its own, which is the case for every family it
    #: declared itself and for every provider that does not discover at all.
    object_mapping: ObjectFieldMapping | None = None
    #: Return only what changed at or after this marker, if the provider can.
    #:
    #: The value is one the provider itself produced, in an earlier call, as a
    #: descriptor's ``source_version["marker"]``; the platform kept the highest it saw
    #: and hands it back. It is opaque here on purpose -- a NetBox timestamp, an
    #: increasing revision, an opaque cursor -- because only the source knows how its own
    #: ordering works.
    #:
    #: ``None`` means read everything, which is what a first sync does, what a provider
    #: without :attr:`~.enums.Capability.DELTA` always does, and what the platform falls
    #: back to on a schedule so that deletions are noticed.
    #:
    #: **At or after, not strictly after.** Two records can carry the same marker, so
    #: excluding the boundary would drop whichever of them the last read did not reach.
    #: Returning one record twice costs an upsert that changes nothing; missing one
    #: leaves the platform wrong until the next full pass.
    since: str | None = None
    #: Filters the instance advertised for this object family, as JSON Schema.
    #:
    #: Kept with the per-family call metadata for the same reason as ``object_mapping``:
    #: a family discovered from one instance may not exist in the provider's shipped
    #: catalogue, and a fresh provider has nowhere else to recover its query contract.
    #: Appended to preserve positional construction of the older context fields.
    object_query_schema: Mapping[str, Any] = field(default_factory=dict)


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
    #: Where this record stood in the source's own ordering when it was read.
    #:
    #: A provider that declares :attr:`~.enums.Capability.DELTA` puts a ``"marker"`` key
    #: here -- NetBox uses its ``last_updated`` -- and the platform keeps the highest
    #: marker it saw across the whole listing, then hands it back as
    #: :attr:`ProviderCallContext.since` next time.
    #:
    #: The platform never parses it. It compares markers as strings and keeps the
    #: greatest, which is correct for any format that sorts lexicographically in its own
    #: order -- an ISO-8601 timestamp does, a zero-padded revision does. A provider whose
    #: markers do not should not declare the capability, because the platform has no way
    #: to tell.
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
    attributes: dict[str, Any] = field(default_factory=dict)
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
        **device.attributes,
        "mgmt_host": device.mgmt_host,
        "mgmt_port": device.mgmt_port,
        "platform": device.platform,
        "hostname": device.hostname,
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
