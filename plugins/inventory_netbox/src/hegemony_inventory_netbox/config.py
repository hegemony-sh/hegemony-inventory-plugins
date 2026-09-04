# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider config schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from hegemony_inventory_sdk import MaintenanceState

# NetBox device statuses -> the platform's MaintenanceState. "Gone or going" is
# disabled; "not yet in service" is maintenance. Anything unlisted (including
# "active") leaves the device active. Override per provider with ``status_map``.
DEFAULT_STATUS_MAP = {
    "offline": "disabled",
    "decommissioning": "disabled",
    "planned": "maintenance",
    "staged": "maintenance",
    "failed": "maintenance",
    "inventory": "maintenance",
}

DEFAULT_FIELD_MAP = {
    "external_id": "id",
    "name": "name",
    "display_name": "display",
    "hostname": "hostname",
    "mgmt_host": "primary_ip4.address",
    "platform": "platform.slug",
    "model": "device_type.model",
    "vendor": "device_type.manufacturer.name",
    # Surfaced under ``descriptive`` — the platform's device row has no column
    # for these, but they are searchable there and visible over the API.
    "serial": "serial",
    "asset_tag": "asset_tag",
    "rack": "rack.name",
    "position": "position",
    "tenant": "tenant.slug",
    "location": "location.slug",
    "mgmt_host_v6": "primary_ip6.address",
    "site.external_id": "site.slug",
    "site.name": "site.name",
    "role": "role.slug",
    "tags": "tags",
    "access_config.ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
    "access_config.ssh.password_ref": "custom_fields.hegemony_ssh_password_ref",
    "access_config.enable.password_ref": "custom_fields.hegemony_enable_password_ref",
    "access_config.ssh.private_key_ref": "custom_fields.hegemony_ssh_private_key_ref",
}


class NetBoxProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    token_ref: str = Field(
        ...,
        description=(
            "Reference to a stored secret holding the NetBox API token, "
            "e.g. {{ secret('vault://netbox/token') }}."
        ),
        json_schema_extra={"x_secret_ref": True},
    )
    auth_scheme: str = Field(
        "Bearer",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="Primary HTTP Authorization scheme prepended to resolved token values.",
    )
    auth_fallback_schemes: list[str] = Field(
        default_factory=lambda: ["Token"],
        max_length=4,
        description="Fallback Authorization schemes retried after auth failures when the token is unprefixed.",
    )
    verify_tls: bool = True
    device_query: str = Field(
        "",
        max_length=2048,
        description=(
            "NetBox device filter applied to every device read, e.g. "
            "'status=active&role=wan-edge'. Auto-sync otherwise pulls the entire "
            "device table. A caller-supplied query is appended to this one."
        ),
    )
    tag_key_prefix: str = Field(
        "tag:",
        max_length=32,
        description=(
            "Prefix for NetBox tag slugs surfaced as filterable device tags, e.g. the "
            "NetBox tag 'core' becomes tags['tag:core'] = 'true'. The prefix keeps a "
            "NetBox tag named 'role' from colliding with the mapped device role; set it "
            "to an empty string to map NetBox tags onto bare keys."
        ),
    )
    status_map: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_STATUS_MAP),
        description=(
            "Maps a NetBox device status slug to a maintenance state "
            "(active, maintenance or disabled). Statuses left out stay active."
        ),
    )
    field_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    default_access_config: dict[str, Any] = Field(default_factory=dict)
    query_cache_ttl_seconds: int | None = Field(None, ge=0, le=86400)
    timeout_seconds: float = Field(10.0, gt=0, le=120)

    @field_validator("token_ref")
    @classmethod
    def token_ref_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("token_ref is required")
        return value.strip()

    @field_validator("auth_scheme", mode="before")
    @classmethod
    def auth_scheme_required(cls, value: Any) -> str:
        if value is None:
            return "Bearer"
        stripped = str(value).strip()
        if not stripped:
            raise ValueError("auth_scheme is required")
        return stripped

    @field_validator("auth_fallback_schemes", mode="before")
    @classmethod
    def normalize_auth_fallback_schemes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            candidates = [item.strip() for item in value.split(",")]
        elif isinstance(value, list):
            candidates = [str(item).strip() for item in value]
        else:
            raise ValueError("auth_fallback_schemes must be a list or comma-separated string")
        return [item for item in candidates if item]

    @field_validator("device_query", mode="before")
    @classmethod
    def normalize_device_query(cls, value: Any) -> str:
        """Accept a leading '?' and surrounding whitespace."""
        if value is None:
            return ""
        return str(value).strip().lstrip("?").strip()

    @field_validator("tag_key_prefix", mode="before")
    @classmethod
    def normalize_tag_key_prefix(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("status_map")
    @classmethod
    def validate_status_map(cls, value: dict[str, str]) -> dict[str, str]:
        """Normalize keys/values and reject states the platform does not model."""
        allowed = {state.value for state in MaintenanceState}
        normalized: dict[str, str] = {}
        for status, state in value.items():
            if not isinstance(status, str) or not isinstance(state, str):
                continue
            status_key = status.strip().lower()
            state_value = state.strip().lower()
            if not status_key or not state_value:
                continue
            if state_value not in allowed:
                raise ValueError(
                    f"status_map['{status_key}'] must be one of {sorted(allowed)}, "
                    f"got '{state_value}'"
                )
            normalized[status_key] = state_value
        return normalized

    @field_validator("field_map")
    @classmethod
    def normalize_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, path in value.items():
            if not isinstance(key, str) or not isinstance(path, str):
                continue
            stripped_key = key.strip()
            stripped_path = path.strip()
            if stripped_key and stripped_path:
                normalized[stripped_key] = stripped_path
        return normalized

    def safe_config(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
