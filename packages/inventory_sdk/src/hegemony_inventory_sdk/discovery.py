# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .mapping import ObjectFieldMapping
from .objecttype import ObjectTypeSpec
from .transforms import normalize_transforms

__all__ = ["TRANSFORMABLE_FIELDS", "DiscoveryResult", "ProjectionBinding"]


#: The binding fields a transform may be declared against.
#:
#: Every one names a single scalar the platform writes into a device or site column.
#: The edges are absent on purpose: an edge's value is an external id, and an id the
#: platform altered on the way past would no longer resolve to the row it was stored
#: under -- which is the exact class of defect projection exists to remove.
TRANSFORMABLE_FIELDS = frozenset(
    {
        "name",
        "hostname",
        "display_name",
        "device_hostname",
        "mgmt_port",
        "platform",
        "serial",
        "location",
        "maintenance_state",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectionBinding:
    """Which stored object family becomes a platform device or site, and how.

    The platform has two first-class tables -- devices and sites -- and they are what
    runs target, what the site tree groups, and what an operator picks in a run form.
    Everything else is a generic object. A provider that serves devices as a family like
    any other says so here, and the platform *projects* the stored rows onto those
    tables instead of asking for them a second time through ``query_devices`` and
    ``list_sites``.

    Reading the stored objects rather than the source twice is the point. Today a NetBox
    device arrives once as a device and its nested site arrives again as a site, keyed a
    second way, which is how one NetBox site came to exist as two platform rows. There is
    nothing to keep consistent if there is only one read.

    Paths are dotted and read the stored object's ``attributes``; ``*_edge`` names a
    relationship the object already carries, so a site's parent is a real edge to a real
    region rather than a name to be matched up later.
    """

    #: ``"device"`` or ``"site"``: the platform table these objects become.
    target: Literal["device", "site"]
    #: The stored object family to read.
    object_type: str
    #: Attribute path for the row's name. Defaults to the object's own name.
    name: str | None = None
    #: Device only: management address, DNS hostname, platform and serial.
    hostname: str | None = None
    #: Additional management-address paths, tried in order after hostname.
    hostname_fallbacks: tuple[str, ...] = ()
    display_name: str | None = None
    device_hostname: str | None = None
    #: Device only: source path and provider-wide fallback for the management port.
    mgmt_port: str | None = None
    default_mgmt_port: int | None = None
    platform: str | None = None
    serial: str | None = None
    #: Site only: location/address text.
    location: str | None = None
    #: Device only: the relationship that reaches this device's site.
    site_edge: str | None = None
    #: Site only: the relationship that reaches this row's parent. The parent may be in
    #: another family -- a NetBox site's parent is a region -- so long as that family is
    #: bound onto sites too, which is how a hierarchy that lives elsewhere becomes the
    #: platform's site tree.
    parent_edge: str | None = None
    #: Prefix for the projected row's external id.
    #:
    #: The platform keys a site on ``(provider_id, external_id)``, and more than one
    #: family can land in the sites table: NetBox sites nest under NetBox regions, and
    #: both become site rows. Slugs are unique per model and not across them, so a region
    #: and a site may both be ``gondor`` -- unprefixed they collide into one row that is
    #: its own parent. A prefix per family makes that impossible rather than unlikely.
    external_id_prefix: str = ""
    #: Device only: attribute path whose value maps onto a maintenance state, with the map.
    maintenance_state: str | None = None
    # Optional "*" entry supplies a default for a present but unmapped source value.
    maintenance_state_map: Mapping[str, str] = field(default_factory=dict)
    #: Attribute paths for access-config secret references, by dotted config key.
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    #: Access config every projected device starts from, before :attr:`secret_refs`.
    #:
    #: Literal values rather than paths: a transport port or a username the whole estate
    #: shares is a property of the provider, not of any one record, and there is nothing
    #: in the source to read it from. What a record does carry wins over it.
    default_access_config: Mapping[str, Any] = field(default_factory=dict)
    #: Attribute paths copied into the row's tags, by tag key.
    tags: Mapping[str, str] = field(default_factory=dict)
    #: Attribute paths copied into the row's descriptive metadata, by output key.
    descriptive: Mapping[str, str] = field(default_factory=dict)
    #: Attribute path to a *list* whose entries each become a tag key set to ``"true"``.
    #:
    #: A source's own tags are usually a flat list of labels, while the platform filters
    #: devices on a ``key=value`` map -- ``tags[role]=core`` in device search, ``tag.*``
    #: in a run form. One path and one key cannot express that: the list has no fixed
    #: length and its entries *are* the keys. So this names the list, and
    #: :attr:`tag_prefix` namespaces what comes out of it.
    #:
    #: Entries may be plain strings or objects; an object contributes its ``slug``, or
    #: its ``name`` where it has no slug, because that is how every source that models a
    #: tag as a record spells the stable half of it.
    tag_list: str | None = None
    #: Attribute path copied into DeviceDescriptor.native_tags. When omitted,
    #: tag_list is reused so source tags retain both native and filter forms.
    native_tags: str | None = None
    #: Prefix for the keys :attr:`tag_list` produces.
    #:
    #: Source tags and mapped fields share one namespace on the row, so a source tag
    #: named ``role`` would otherwise overwrite the mapped role. An empty prefix is
    #: allowed and means the operator has accepted that.
    tag_prefix: str = ""
    #: Named conversions to apply to a projected value, by the binding field producing it.
    #:
    #: Keyed by the name of the field on this binding -- ``"hostname"``, ``"platform"`` --
    #: rather than by the attribute path, because the same stored attribute can feed two
    #: fields that want it differently, and because a path is the provider's business
    #: while this says what the *platform column* needs.
    #:
    #: This is what keeps the stored object and the projected row from having to agree.
    #: A NetBox interface address is genuinely ``10.0.0.1/24`` and the object page should
    #: show that; a device's management address has to be something a transport can
    #: dial. One read, two truthful answers, and the difference declared here rather than
    #: hard-coded in whichever consumer noticed first.
    #:
    #: Values are transform names from :data:`~.transforms.TRANSFORMS`, applied in the
    #: order written, and are always a list even for a single name -- a bare string is
    #: refused because ``str`` is itself a sequence of strings and would otherwise be
    #: read one character at a time. A name this platform does not know raises at
    #: construction, which surfaces as a failed discovery naming the field; see that
    #: module for why silence would be worse.
    #:
    #: Each field needs a declared source path; hostname fallback paths also qualify.
    #: Implicit row-name and management-port defaults are applied after transforms
    #: by the host, so they do not supply a transform input.
    #:
    #: Normalised to tuples on construction, so a binding restored from JSON -- where a
    #: tuple can only come back as a list -- compares equal to the one that was stored.
    transforms: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject fields and transforms the selected projection cannot consume."""
        if self.target not in ("device", "site"):
            raise ValueError(
                f"ProjectionBinding target must be 'device' or 'site': {self.target!r}"
            )
        if self.target == "site" and (self.hostname or self.device_hostname or self.site_edge):
            raise ValueError(
                "ProjectionBinding: a site has no hostname, device hostname, or site edge"
            )
        if self.target == "device" and (self.parent_edge or self.location):
            raise ValueError("ProjectionBinding: site-only fields are set on a device binding")
        if self.target == "site" and (
            self.hostname_fallbacks
            or self.display_name
            or self.mgmt_port
            or self.default_mgmt_port is not None
            or self.native_tags
            or self.platform
            or self.serial
            or self.secret_refs
            or self.default_access_config
            or self.maintenance_state
            or self.maintenance_state_map
        ):
            raise ValueError("ProjectionBinding: device-only fields are set on a site binding")
        if self.default_mgmt_port is not None and not 1 <= self.default_mgmt_port <= 65535:
            raise ValueError("ProjectionBinding: default_mgmt_port must be between 1 and 65535")
        object.__setattr__(self, "hostname_fallbacks", tuple(self.hostname_fallbacks))
        if self.tag_prefix and not self.tag_list:
            # A prefix with nothing to prefix is a mapping that does not do what its
            # author thinks it does, which is worth failing rather than ignoring.
            raise ValueError("ProjectionBinding: tag_prefix is set but tag_list is not")
        if self.transforms:
            unknown_fields = sorted(set(self.transforms) - TRANSFORMABLE_FIELDS)
            if unknown_fields:
                # A transform on a field that produces no value is a declaration that
                # can never run. Naming the field it should have been on is the whole
                # help available here, so the error carries the set.
                raise ValueError(
                    f"ProjectionBinding: transforms name no projected field: "
                    f"{', '.join(unknown_fields)}; "
                    f"projected fields are: {', '.join(sorted(TRANSFORMABLE_FIELDS))}"
                )
            missing_sources = sorted(
                name
                for name in self.transforms
                if not getattr(self, name)
                and not (name == "hostname" and any(self.hostname_fallbacks))
            )
            if missing_sources:
                # Row-name and port defaults are applied after transforms by the host.
                # Only hostname fallbacks pass through the declared transform chain.
                raise ValueError(
                    "ProjectionBinding: transforms require a source: " + ", ".join(missing_sources)
                )
            object.__setattr__(
                self,
                "transforms",
                normalize_transforms(self.transforms, where="ProjectionBinding.transforms"),
            )


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
    #: Which of those families are the platform's devices and sites. Empty means this
    #: provider still answers ``query_devices`` and ``list_sites`` itself, which is what
    #: every provider did before projection existed and what a provider that has not
    #: been changed keeps doing.
    bindings: tuple[ProjectionBinding, ...] = ()
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
        # A binding on a family nobody described projects from rows that never arrive.
        unbound = sorted(
            binding.object_type for binding in self.bindings if binding.object_type not in declared
        )
        if unbound:
            raise ValueError(
                "DiscoveryResult binds object types it does not declare, so nothing "
                f"would ever be projected from them: {unbound}"
            )
        families = [binding.object_type for binding in self.bindings]
        if len(set(families)) != len(families):
            raise ValueError(
                "DiscoveryResult binds one object type more than once; its rows would be "
                "projected twice and keep whichever binding ran last"
            )
        # Several families may become sites -- that is how a hierarchy living in another
        # family becomes the site tree -- but only one can become devices: a device row
        # written from two families keeps whichever ran last.
        if len([binding for binding in self.bindings if binding.target == "device"]) > 1:
            raise ValueError("DiscoveryResult binds more than one object type onto devices")
        prefixes = [
            binding.external_id_prefix for binding in self.bindings if binding.target == "site"
        ]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError(
                "DiscoveryResult binds two object types onto sites under the same external "
                "id prefix; their rows would collide on (provider_id, external_id)"
            )
