# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git inventory provider config schema."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GitInventoryProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_repo_id: UUID = Field(
        ...,
        description="The configured Git repository to read inventory from (Settings → Git Repositories).",
        json_schema_extra={"x_reference": "git_repository"},
    )
    branch: str = Field("main", min_length=1, max_length=255)
    path: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="Repository-relative path to the inventory directory containing sites/ and devices/.",
    )
    default_access_config: dict[str, Any] = Field(default_factory=dict)
    refresh_strategy: Literal["cache_ttl"] = "cache_ttl"

    @model_validator(mode="before")
    @classmethod
    def drop_retired_cache_ttl(cls, value: Any) -> Any:
        if isinstance(value, dict) and "query_cache_ttl_seconds" in value:
            value = dict(value)
            value.pop("query_cache_ttl_seconds", None)
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("branch is required")
        if (
            stripped.startswith("-")
            or ".." in stripped
            or any(ch in stripped for ch in " ~^:?*[\\")
        ):
            raise ValueError("branch contains unsafe characters")
        return stripped

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("path must be a safe repository-relative path")
        path = PurePosixPath(stripped)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(part.startswith(".") for part in path.parts)
        ):
            raise ValueError("path must be a safe repository-relative path")
        return str(path).rstrip("/")

    def safe_config(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
