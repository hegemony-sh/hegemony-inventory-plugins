# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Inventory plugin enums.

These are the canonical definitions; ``packages/core/enums.py`` re-exports them so
existing ``from packages.core.enums import Capability`` imports keep the same object
identity. This module must not import any framework.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """Inventory provider capabilities."""

    READ = "read"
    QUERY = "query"
    WRITE = "write"
    WEBHOOK = "webhook"
    #: The provider can describe the instance it is configured against, rather than only
    #: the object types its plugin was written to know about. A source of truth that can
    #: be extended -- NetBox with plugins installed, custom fields defined -- serves
    #: object types no shipped catalogue could have anticipated, and only the instance
    #: can say what they are.
    DISCOVER = "discover"
    #: The provider can answer "what changed since X" rather than only "what is there".
    #:
    #: Declaring it is a promise about two things at once. The provider honours
    #: ``ProviderCallContext.since`` by returning every record that changed at or after
    #: it -- a record missed is a record the platform will not look at again until the
    #: next full pass -- and it stamps each descriptor's ``source_version`` with a marker
    #: the platform can carry back. A provider that does one without the other is worse
    #: than one that does neither, because the platform would believe it was up to date.
    DELTA = "delta"


class ResourceType(str, Enum):
    """Built-in inventory object-type identifiers.

    Object types are an open, registry-validated string space; these are the
    well-known built-in ids. Plugins may register additional object types.
    """

    DEVICE = "device"
    SITE = "site"
    IP_PREFIX = "ip_prefix"
    IP_ADDRESS = "ip_address"
    VLAN = "vlan"
    CONTACT = "contact"


class ProviderErrorCode(str, Enum):
    """Stable provider error envelope codes."""

    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    TLS_FAILED = "tls_failed"
    NOT_FOUND = "not_found"
    PAGINATION_FAILED = "pagination_failed"
    MALFORMED_QUERY = "malformed_query"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNAVAILABLE = "unavailable"


class MaintenanceState(str, Enum):
    """Local maintenance marker for inventory identity rows."""

    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DISABLED = "disabled"
