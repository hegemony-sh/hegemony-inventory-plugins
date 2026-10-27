# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SDK-only test doubles for provider unit tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from hegemony_inventory_sdk import GitFetcher, GitWorkdir, InventoryLimits, SecretRef


class FakeGitFetcher:
    """Returns a fixed working tree without real Git transport."""

    def __init__(self, *, path: str, head_sha: str = "0" * 40) -> None:
        self._workdir = GitWorkdir(path=path, head_sha=head_sha)

    async def get_workdir(
        self, *, git_repo_id: str, branch: str | None, operation: str
    ) -> GitWorkdir:
        return self._workdir


class FakePlatformServices:
    """In-memory ``PlatformServices`` for provider unit tests."""

    limits: InventoryLimits
    git: GitFetcher

    def __init__(
        self,
        *,
        token: str | None = None,
        provider_id: str = "test:primary",
        provider_type: str = "test",
        limits: InventoryLimits | None = None,
        git: GitFetcher | None = None,
    ) -> None:
        self.token = token
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.limits = limits or InventoryLimits(
            max_provider_pages=100,
            max_run_targets=1000,
            max_preview_devices=500,
        )
        self.git = git or FakeGitFetcher(path="/nonexistent")

    async def resolve_secret_ref(self, ref: SecretRef | None, *, operation: str) -> str | None:
        return self.token

    async def validate_url(self, url: str, *, operation: str) -> str:
        return urlparse(url).hostname or ""

    def validate_access_config(
        self,
        access_config: Mapping[str, Any] | None,
        *,
        operation: str,
        allow_raw_literals: bool = False,
    ) -> dict[str, Any]:
        return _drop_empty_values(access_config)


def _drop_empty_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned = {
            str(key): _drop_empty_values(item)
            for key, item in value.items()
            if item not in (None, "")
        }
        return {key: item for key, item in cleaned.items() if item != {} and item not in (None, "")}
    return value
