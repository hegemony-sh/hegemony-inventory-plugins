# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git YAML inventory provider."""

from __future__ import annotations

import builtins
import copy
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator

from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    DiscoveryResult,
    GitWorkdir,
    InventoryProvider,
    InventoryProviderError,
    ObjectDescriptor,
    ObjectRelationship,
    PlatformServices,
    ProviderCallContext,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderTestResult,
    ResourceRef,
    ResourceType,
    SiteRef,
)
from hegemony_inventory_sdk.mapping import extract_path

from .config import GitInventoryProviderConfig
from .loader import LoadedGitInventory, LoadedGitSite, load_git_inventory
from .query import device_tags, matches_query


class GitInventoryProvider(InventoryProvider):
    provider_type = "git"
    capabilities = frozenset({Capability.READ, Capability.QUERY, Capability.DISCOVER})
    supported_resources = frozenset({ResourceType.DEVICE, ResourceType.SITE})
    config_schema = GitInventoryProviderConfig.model_json_schema()

    def __init__(
        self, *, provider_id: str, config: dict[str, Any], services: PlatformServices
    ) -> None:
        self.id = provider_id
        self.config = GitInventoryProviderConfig.model_validate(config or {})
        self._services = services
        self._sync_snapshot: tuple[GitWorkdir, LoadedGitInventory] | None = None

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
        entry, loaded = await self._snapshot(context)

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
        entry, loaded = await self._snapshot(context)
        result_limit = limit
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
            if result_limit is not None and len(descriptors) >= result_limit:
                break
        return descriptors

    async def list_sites(
        self, *, limit: int | None = None, context: ProviderCallContext
    ) -> builtins.list[SiteRef]:
        entry, loaded = await self._snapshot(context)
        return self._site_refs_from_loaded(loaded, limit=limit)

    async def test_connection(self, *, context: ProviderCallContext) -> ProviderTestResult:
        start = monotonic()
        try:
            entry, loaded = await self._snapshot(context)
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
            details = {
                "head_sha": entry.head_sha,
                "device_count": len(loaded.devices),
                "object_type_count": len(loaded.generic.specs),
                "object_count": sum(len(records) for records in loaded.generic.records.values()),
            }
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

    async def _snapshot(
        self, context: ProviderCallContext
    ) -> tuple[GitWorkdir, LoadedGitInventory]:
        if context.operation.startswith("auto_sync") and self._sync_snapshot is not None:
            return self._sync_snapshot

        def read(entry: GitWorkdir) -> tuple[GitWorkdir, LoadedGitInventory]:
            loaded = load_git_inventory(
                Path(entry.path),
                self.config.path,
                max_files=self._services.limits.max_git_files,
                max_file_bytes=self._services.limits.max_git_file_bytes,
            )
            self._raise_load_errors(loaded, context.operation)
            return entry, loaded

        # New hosts lend a locked checkout while it is parsed into owned memory.
        # Legacy hosts remain usable for site/device inventories only.
        locked = getattr(self._services.git, "locked_workdir", None)
        if callable(locked):
            async with locked(
                git_repo_id=str(self.config.git_repo_id),
                branch=self.config.branch,
                operation=context.operation,
            ) as entry:
                result = read(entry)
        else:
            result = read(await self._cache_entry(context))
            if result[1].generic.definitions:
                raise self._error(
                    ProviderErrorCode.MALFORMED_QUERY,
                    "Generic Git objects require platform Git read-lock support",
                    context.operation,
                )
        return result

    async def discover(self, *, context: ProviderCallContext) -> DiscoveryResult:
        self._sync_snapshot = None
        snapshot = await self._snapshot(context)
        if context.operation == "discover_cached":
            # Core constructs a fresh provider per sync. A subsequent discovery
            # resets this snapshot; unrelated reads never reuse it.
            self._sync_snapshot = snapshot
        generic = snapshot[1].generic
        return DiscoveryResult(
            source_version=generic.fingerprint,
            object_types=tuple(generic.specs[key] for key in sorted(generic.specs)),
            mappings={key: definition.mapping() for key, definition in generic.definitions.items()},
        )

    async def _objects(
        self, object_type: str, query: Mapping[str, Any] | None, context: ProviderCallContext
    ) -> builtins.list[ObjectDescriptor]:
        entry, loaded = await self._snapshot(context)
        generic = loaded.generic
        spec = generic.specs.get(object_type)
        if spec is None:
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "Git object type is not supported",
                context.operation,
            )
        query = dict(query or {})
        if not Draft202012Validator(dict(spec.query_schema)).is_valid(query):
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "Unsupported or invalid object filter",
                context.operation,
            )
        results = []
        for record in generic.records[object_type].values():
            if any(
                getattr(record, key) != query[key]
                for key in ("name", "external_id")
                if key in query
            ):
                continue
            if any(
                not self._equal_attribute(record.attributes, key[5:], value)
                for key, value in query.items()
                if key.startswith("attr.")
            ):
                continue
            search = query.get("q", "").casefold()
            searchable = [
                record.name,
                record.display_name,
                record.external_id,
                *(extract_path(record.attributes, key) for key in spec.ui.search_fields),
            ]
            if search and not any(
                search in str(value).casefold() for value in searchable if value is not None
            ):
                continue
            results.append(
                ObjectDescriptor(
                    provider_id=self.id,
                    object_type=object_type,
                    external_id=record.external_id,
                    name=record.name,
                    display_name=record.display_name,
                    attributes=copy.deepcopy(record.attributes),
                    relationships=tuple(
                        ObjectRelationship(
                            rel=edge.rel,
                            target_object_type=edge.target_object_type,
                            target_external_id=edge.target_external_id,
                        )
                        for edge in record.relationships
                    ),
                    source_version={"commit": entry.head_sha},
                    descriptive={"file_path": generic.files[(object_type, record.external_id)]},
                )
            )
        results.sort(key=lambda obj: obj.external_id or "")
        sort = spec.ui.default_sort
        if sort:
            key = sort.lstrip("-")

            def sort_key(obj: ObjectDescriptor) -> tuple[bool, str, Any]:
                value = (
                    getattr(obj, key)
                    if key in {"name", "display_name", "external_id"}
                    else extract_path(obj.attributes, key)
                )
                return (
                    value is None,
                    "number" if type(value) in (int, float) else type(value).__name__,
                    value if isinstance(value, (str, int, float)) else str(value),
                )

            results.sort(key=sort_key, reverse=sort.startswith("-"))
        return results

    @staticmethod
    def _equal_attribute(attributes: dict[str, Any], path: str, value: Any) -> bool:
        current: Any = attributes
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        # Python considers True == 1; JSON typed equality must not.
        return current == value and (isinstance(current, bool) == isinstance(value, bool))

    async def list_objects(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        context: ProviderCallContext,
    ) -> builtins.list[ObjectDescriptor]:
        if cursor is not None or (limit is not None and (type(limit) is not int or limit < 0)):
            raise self._error(
                ProviderErrorCode.MALFORMED_QUERY,
                "Cursors are unsupported; use iter_object_pages for complete enumeration",
                context.operation,
            )
        if object_type in {"device", "site"}:
            return await super().list_objects(object_type, query, limit=limit, context=context)
        objects = await self._objects(object_type, query, context)
        return objects if limit is None else objects[:limit]

    async def get_object(
        self, object_type: str, external_id: str, *, context: ProviderCallContext
    ) -> ObjectDescriptor:
        if object_type in {"device", "site"}:
            return await super().get_object(object_type, external_id, context=context)
        objects = await self._objects(object_type, {"external_id": external_id}, context)
        if not objects:
            raise self._error(
                ProviderErrorCode.NOT_FOUND, "Git object not found", context.operation
            )
        return objects[0]

    async def iter_object_pages(
        self,
        object_type: str,
        query: Mapping[str, Any] | None = None,
        *,
        context: ProviderCallContext,
    ) -> AsyncIterator[builtins.list[ObjectDescriptor]]:
        objects = await self.list_objects(object_type, query, context=context)
        page_size = 500
        if len(objects) > page_size * self._services.limits.max_provider_pages:
            raise self._error(
                ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                "Object page count limit exceeded",
                context.operation,
            )
        for offset in range(0, len(objects), page_size):
            yield objects[offset : offset + page_size]

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
        descriptive = copy.deepcopy(getattr(site_model.model, "custom_fields", {}) or {})
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
            "custom_fields": copy.deepcopy(device.custom_fields),
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
            attributes=copy.deepcopy(device.attributes),
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
