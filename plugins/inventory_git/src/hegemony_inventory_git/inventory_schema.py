# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public Git inventory YAML schema v1."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitInventorySiteV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["site"]
    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    location: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("external_id", "name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

    @field_validator("external_id")
    @classmethod
    def validate_external_id_segment(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("external_id must describe only the current site path segment")
        return value

    @field_validator("description", "location")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str):
                continue
            stripped_key = key.strip()
            if not stripped_key:
                continue
            text_value = str(value).strip()
            if text_value:
                normalized[stripped_key] = text_value
        return normalized


class GitInventoryJumpHostV1(BaseModel):
    """A per-device bastion hop.

    Its own scope rather than a ``dict[str, str | None]`` like ``ssh`` and
    ``enable``, because it mixes credential refs with two plain literals —
    and ``port`` is an int, which the credential-scope type cannot express.
    Fields mirror the platform's access-config validation exactly (secret
    refs plus the ``host``/``port`` literals it carries through); anything
    else is rejected here rather than silently dropped downstream.
    """

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = Field(None, ge=1, le=65535)
    username_ref: str | None = None
    password_ref: str | None = None
    private_key_ref: str | None = None


class GitInventoryAccessConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssh: dict[str, str | None] = Field(default_factory=dict)
    enable: dict[str, str | None] = Field(default_factory=dict)
    # A device may name its own bastion (a site behind a different jump host)
    # or omit the block and inherit the provider's default_access_config —
    # the same per-device override the ssh/enable scopes have always had.
    jump_host: GitInventoryJumpHostV1 = Field(default_factory=GitInventoryJumpHostV1)


class GitInventoryDeviceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["device"]
    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mgmt_host: str = Field(min_length=1)
    # Optional: omit to let the core inventory service apply its default management port.
    mgmt_port: int | None = Field(None, ge=1, le=65535)
    # Optional: omit to let the core inventory service apply its default platform.
    platform: str | None = None
    vendor: str | None = None
    model: str | None = None
    site: str | None = None
    role: str | None = None
    tags: list[str] = Field(default_factory=list)
    access_config: GitInventoryAccessConfigV1 = Field(default_factory=GitInventoryAccessConfigV1)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("external_id", "name", "mgmt_host")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

    @field_validator("site", "role", mode="before")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("tags")
    @classmethod
    def strip_tags(cls, values: list[str]) -> list[str]:
        return [item.strip() for item in values if isinstance(item, str) and item.strip()]
