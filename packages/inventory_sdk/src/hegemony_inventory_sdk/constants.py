# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared inventory constants used by SDK value objects."""

from __future__ import annotations

# Default device management (SSH) port and platform. Mirror ``packages.core.defaults``
# for the value objects and providers built against the SDK, keeping it app-free.
DEFAULT_DEVICE_MGMT_PORT = 22
DEFAULT_DEVICE_PLATFORM = "ios-xe"
