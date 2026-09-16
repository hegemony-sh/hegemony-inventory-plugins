# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Infrahub inventory provider config schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

DEFAULT_FIELD_MAP = {
    "external_id": "id",
    "name": "name.value",
    "mgmt_host": "primary_address.node.address.value",
    "platform": "platform.node.name.value",
    "site": "site.node.name.value",
    "role": "role.value",
    "tags": "tags.edges.node.name.value",
}


class InfrahubProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    token_ref: str = Field(
        ...,
        description=(
            "Reference to a stored secret holding the Infrahub API token, "
            "e.g. {{ secret('vault://infrahub/token') }}."
        ),
        json_schema_extra={"x_secret_ref": True},
    )
    verify_tls: bool = True
    branch: str = Field("main", min_length=1, max_length=255)
    device_kind: str = Field("InfraDevice", min_length=1, max_length=255)
    field_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    default_access_config: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(10.0, gt=0, le=120)

    @model_validator(mode="before")
    @classmethod
    def drop_retired_cache_ttl(cls, value: Any) -> Any:
        if isinstance(value, dict) and "query_cache_ttl_seconds" in value:
            value = dict(value)
            value.pop("query_cache_ttl_seconds", None)
        return value

    @field_validator("token_ref")
    @classmethod
    def token_ref_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("token_ref is required")
        return stripped

    @field_validator("branch", "device_kind")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value is required")
        return stripped

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
