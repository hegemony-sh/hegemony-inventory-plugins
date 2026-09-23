# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared device access-configuration contract.

Providers, the API, and workers all exchange this JSON shape. Keeping the shape in
the SDK prevents one layer from accepting a field such as ssh.transport that another
silently discards.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SSHAccessConfig(BaseModel):
    """SSH credentials and transport selection for a device."""

    model_config = ConfigDict(extra="forbid")

    username_ref: str | None = None
    password_ref: str | None = None
    private_key_ref: str | None = None
    transport: str | None = Field(default=None, min_length=1, max_length=64)
    # Compatibility input for existing provider configs. The platform promotes it to
    # Device.mgmt_port; new providers should set descriptor.mgmt_port directly.
    port: int | None = Field(default=None, ge=1, le=65535)


class EnableAccessConfig(BaseModel):
    """Privilege-escalation credentials."""

    model_config = ConfigDict(extra="forbid")

    password_ref: str | None = None


class JumpHostAccessConfig(BaseModel):
    """One SSH bastion used to reach a device."""

    model_config = ConfigDict(extra="forbid")

    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username_ref: str | None = None
    password_ref: str | None = None
    private_key_ref: str | None = None


class AccessConfigModel(BaseModel):
    """Canonical JSON-compatible access configuration."""

    model_config = ConfigDict(extra="forbid")

    ssh: SSHAccessConfig | None = None
    enable: EnableAccessConfig | None = None
    jump_host: JumpHostAccessConfig | None = None


ACCESS_CONFIG_JSON_SCHEMA = AccessConfigModel.model_json_schema()


__all__ = [
    "ACCESS_CONFIG_JSON_SCHEMA",
    "AccessConfigModel",
    "EnableAccessConfig",
    "JumpHostAccessConfig",
    "SSHAccessConfig",
]
