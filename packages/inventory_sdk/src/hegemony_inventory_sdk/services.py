# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Platform services injected into providers at construction.

An out-of-tree provider never imports the platform's secret backends, SSRF policy, Git
cache, or settings. The core platform builds a :class:`PlatformServices` bound to the
request's DB session and the provider's id/type, and passes it to the provider. The
provider calls these methods instead of importing app internals; resolution, validation,
and Git transport all stay inside the platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .secrets import SecretRef


@dataclass(frozen=True, slots=True)
class InventoryLimits:
    """Platform-configured scale limits a provider must honor."""

    max_provider_pages: int
    max_run_targets: int
    max_preview_devices: int
    # Git-backed provider DoS guards (defaults mirror the platform settings).
    max_git_files: int = 50000
    max_git_file_bytes: int = 1_048_576


@dataclass(frozen=True, slots=True)
class GitWorkdir:
    """A checked-out, pinned Git working tree on the local filesystem."""

    path: str
    head_sha: str


@runtime_checkable
class GitFetcher(Protocol):
    """Git read transport the platform provides to Git-backed providers.

    The platform resolves the repository, its auth, and its cache, and returns a
    pinned working tree the provider reads files from.
    """

    async def get_workdir(
        self, *, git_repo_id: str, branch: str | None, operation: str
    ) -> GitWorkdir: ...


@runtime_checkable
class PlatformServices(Protocol):
    """Injected platform capabilities, bound to one provider for one request."""

    limits: InventoryLimits
    git: GitFetcher

    async def resolve_secret_ref(self, ref: SecretRef | None, *, operation: str) -> str | None:
        """Resolve an opaque secret reference to its value (platform-side)."""
        ...

    async def validate_url(self, url: str, *, operation: str) -> str:
        """Validate an http(s) provider URL against SSRF allow-lists; returns the host."""
        ...

    def validate_access_config(
        self,
        access_config: Mapping[str, Any] | None,
        *,
        operation: str,
        allow_raw_literals: bool = False,
    ) -> dict[str, Any]:
        """Validate scoped access-config refs are protected templates (fail-closed)."""
        ...
