# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git YAML inventory provider."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    GitWorkdir,
    InventoryProvider,
    InventoryProviderError,
    PlatformServices,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderTestResult,
    ResourceRef,
    ResourceType,
    SiteRef,
)

from .config import GitInventoryProviderConfig
from .loader import LoadedGitSite, load_git_inventory
from .query import device_tags, matches_query


class GitInventoryProvider(InventoryProvider):
    provider_type = "git"
    capabilities = frozenset({Capability.READ, Capability.QUERY})
    supported_resources = frozenset({ResourceType.DEVICE, ResourceType.SITE})
    config_schema = GitInventoryProviderConfig.model_json_schema()

    def __init__(
        self, *, provider_id: str, config: dict[str, Any], services: PlatformServices
    ) -> None:
        self.id = provider_id
        self.config = GitInventoryProviderConfig.model_validate(config or {})
        self._services = services

    async def list(
        self,
        resource_type: ResourceType,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ResourceRef]:
        if resource_type == ResourceType.DEVICE:
            return [
                ResourceRef(
                    provider_id=d.provider_id,
                    resource_type=ResourceType.DEVICE,
                    external_id=d.external_id or d.name,
                    display_name=d.safe_display_name,
                    descriptive=d.descriptive,
                )
                for d in await self.query_devices("", limit=limit, context=context)
            ]
        if resource_type == ResourceType.SITE:
            return [
                ResourceRef(
                    provider_id=s.provider_id,
                    resource_type=ResourceType.SITE,
                    external_id=s.external_id,
                    display_name=s.name,
                    descriptive=s.descriptive,
                )
                for s in await self.list_sites(limit=limit, context=context)
            ]
        return []

    async def get(
        self, resource_type: ResourceType, external_id: str, *, context: ProviderCallContext
    ) -> DeviceDescriptor | SiteRef | ResourceRef:
        entry = await self._cache_entry(context)
        loaded = load_git_inventory(
            Path(entry.path),
            self.config.path,
            max_files=self._services.limits.max_git_files,
            max_file_bytes=self._services.limits.max_git_file_bytes,
        )
        self._raise_load_errors(loaded, context.operation)

        if resource_type == ResourceType.DEVICE:
            for device in loaded.devices:
                if device.external_id == external_id:
                    return self._map_device(
                        device,
                        loaded.sites,
                        loaded.device_files.get(device.external_id),
                        entry.head_sha,
                    )
        elif resource_type == ResourceType.SITE:
            site = loaded.sites.get(external_id)
            if site is not None:
                return self._site_ref_from_model(site)
        else:
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "Git inventory resource type is not supported",
                context.operation,
            )
        raise self._error(
            ProviderErrorCode.NOT_FOUND, "Git inventory resource not found", context.operation
        )

    async def query_devices(
        self, expr: str, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[DeviceDescriptor]:
        entry = await self._cache_entry(context)
        loaded = load_git_inventory(
            Path(entry.path),
            self.config.path,
            max_files=self._services.limits.max_git_files,
            max_file_bytes=self._services.limits.max_git_file_bytes,
        )
        self._raise_load_errors(loaded, context.operation)
        result_limit = limit or self._services.limits.max_run_targets
        descriptors: list[DeviceDescriptor] = []
        for device in loaded.devices:
            if matches_query(device, loaded.sites, expr):
                descriptors.append(
                    self._map_device(
                        device,
                        loaded.sites,
                        loaded.device_files.get(device.external_id),
                        entry.head_sha,
                    )
                )
            if len(descriptors) >= result_limit:
                break
        return descriptors

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        entry = await self._cache_entry(context)
        loaded = load_git_inventory(
            Path(entry.path),
            self.config.path,
            max_files=self._services.limits.max_git_files,
            max_file_bytes=self._services.limits.max_git_file_bytes,
        )
        self._raise_load_errors(loaded, context.operation)
        result_limit = limit or self._services.limits.max_preview_devices
        return self._site_refs_from_loaded(loaded, limit=result_limit)

    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        start = monotonic()
        try:
            entry = await self._cache_entry(context)
            loaded = load_git_inventory(
                Path(entry.path),
                self.config.path,
                max_files=self._services.limits.max_git_files,
                max_file_bytes=self._services.limits.max_git_file_bytes,
            )
            if loaded.errors:
                raise self._error(
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                    "Git inventory validation failed",
                    "test_connection",
                    safe_details={"errors": [asdict(error) for error in loaded.errors]},
                )
            ok = True
            error = None
            message = "Git inventory connection succeeded"
            details = {"head_sha": entry.head_sha, "device_count": len(loaded.devices)}
        except InventoryProviderError as exc:
            ok = False
            error = exc.envelope.to_dict()
            message = exc.envelope.message
            details = exc.envelope.safe_details
        return ProviderTestResult(
            ok=ok,
            provider_id=self.id,
            provider_type=self.provider_type,
            latency_ms=int((monotonic() - start) * 1000),
            message=message,
            error=error,
            safe_details=details,
        )

    def _raise_load_errors(self, loaded: Any, operation: str) -> None:
        if loaded.errors:
            raise self._error(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "Git inventory validation failed",
                operation,
                safe_details={"errors": [asdict(error) for error in loaded.errors]},
            )

    def _site_refs_from_loaded(
        self, loaded: Any, *, limit: int | None = None
    ) -> builtins.list[SiteRef]:
        refs = [self._site_ref_from_model(site) for site in loaded.sites.values()]
        refs.sort(key=lambda site: (tuple(site.external_id.split("/")), site.name))
        if limit is not None:
            return refs[:limit]
        return refs

    async def _cache_entry(self, context: ProviderCallContext) -> GitWorkdir:
        return await self._services.git.get_workdir(
            git_repo_id=str(self.config.git_repo_id),
            branch=self.config.branch,
            operation=context.operation,
        )

    def _merged_access_config(self, device: Any) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        defaults = (
            self.config.default_access_config
            if isinstance(self.config.default_access_config, Mapping)
            else {}
        )
        device_access_config = device.access_config.model_dump(mode="json")
        # jump_host is a typed model, so an omitted block still dumps every
        # field as None. Those Nones mean "not specified" and must not erase
        # the provider default — unlike the ssh/enable scopes, whose free-form
        # dicts carry only the keys a device file actually wrote (there an
        # explicit null is a deliberate "clear the inherited value").
        jump_host = device_access_config.get("jump_host")
        if isinstance(jump_host, Mapping):
            device_access_config["jump_host"] = {
                key: value for key, value in jump_host.items() if value is not None
            }

        # jump_host rides along with the credential scopes so a provider can
        # stamp a shared bastion hop on every device it yields
        # (design-dind-sandbox.md §11.2 in the platform repo), while a device
        # behind a different bastion overrides it field by field.
        for scope in ("ssh", "enable", "jump_host"):
            scope_values: dict[str, Any] = {}

            default_scope = defaults.get(scope)
            if isinstance(default_scope, Mapping):
                scope_values.update(default_scope)

            device_scope = device_access_config.get(scope)
            if isinstance(device_scope, Mapping):
                scope_values.update(device_scope)

            if scope_values:
                merged[scope] = scope_values

        return merged

    def _site_ref_from_model(self, site_model: LoadedGitSite) -> SiteRef:
        descriptive = dict(getattr(site_model.model, "custom_fields", {}) or {})
        description = getattr(site_model.model, "description", None)
        if description:
            descriptive["description"] = description
        return SiteRef(
            provider_id=self.id,
            external_id=site_model.external_id,
            name=site_model.model.name,
            parent_external_id=site_model.parent_external_id,
            location=getattr(site_model.model, "location", None),
            tags=dict(getattr(site_model.model, "tags", {}) or {}),
            descriptive=descriptive,
        )

    def _map_device(self, device, sites, file_path: str | None, head_sha: str) -> DeviceDescriptor:
        site_model = sites.get(device.site) if device.site else None
        site = self._site_ref_from_model(site_model) if site_model else None
        tags = device_tags(device)
        access_config = self._services.validate_access_config(
            self._merged_access_config(device),
            operation="query_devices",
            allow_raw_literals=False,
        )
        source_version = {
            "head_sha": head_sha,
            "branch": self.config.branch,
            "path": self.config.path,
            "file_path": file_path,
        }
        descriptive = {
            "custom_fields": device.custom_fields,
            "source_version": source_version,
            "native_tags": tags,
        }
        return DeviceDescriptor(
            provider_id=self.id,
            external_id=device.external_id,
            name=device.name,
            display_name=device.name,
            hostname=getattr(device, "hostname", None) or device.name,
            mgmt_host=device.mgmt_host,
            mgmt_port=device.mgmt_port,
            platform=device.platform,
            vendor=device.vendor,
            model=device.model,
            site=site,
            role=device.role,
            tags={"role": device.role} if device.role else {},
            native_tags=tuple(tags),
            access_config=access_config,
            descriptive=descriptive,
            source_version=source_version,
        )

    def _error(
        self,
        code: ProviderErrorCode,
        message: str,
        operation: str,
        *,
        retryable: bool = False,
        safe_details: dict[str, Any] | None = None,
    ) -> InventoryProviderError:
        return InventoryProviderError(
            ProviderErrorEnvelope(
                provider_id=self.id,
                provider_type=self.provider_type,
                operation=operation,
                code=code,
                message=message,
                retryable=retryable,
                safe_details=safe_details or {},
            )
        )
