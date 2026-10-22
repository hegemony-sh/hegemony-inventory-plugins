# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git inventory YAML loader with fail-closed validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
import yaml.resolver
from pydantic import ValidationError

from .inventory_schema import GitInventoryDeviceV1, GitInventorySiteV1


class DuplicateKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.Loader, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


@dataclass(slots=True)
class GitInventoryLoadError:
    file_path: str
    message: str


@dataclass(slots=True)
class LoadedGitSite:
    external_id: str
    parent_external_id: str | None
    model: GitInventorySiteV1
    file_path: str


@dataclass(slots=True)
class LoadedGitInventory:
    devices: list[GitInventoryDeviceV1]
    sites: dict[str, LoadedGitSite]
    device_files: dict[str, str]
    errors: list[GitInventoryLoadError] = field(default_factory=list)


def load_git_inventory(
    root: Path,
    inventory_path: str,
    *,
    max_files: int = 50000,
    max_file_bytes: int = 1_048_576,
) -> LoadedGitInventory:
    root_resolved = root.resolve()
    base_candidate = root / inventory_path
    if base_candidate.is_symlink():
        return LoadedGitInventory(
            [], {}, {}, [GitInventoryLoadError(inventory_path, "Inventory path is a symlink")]
        )
    base = base_candidate.resolve()
    if root_resolved not in base.parents and base != root_resolved:
        return LoadedGitInventory(
            [], {}, {}, [GitInventoryLoadError(inventory_path, "Inventory path escapes repository")]
        )
    if not base.exists() or not base.is_dir():
        return LoadedGitInventory(
            [], {}, {}, [GitInventoryLoadError(inventory_path, "Inventory path does not exist")]
        )
    devices_dir = base / "devices"
    sites_dir = base / "sites"
    errors: list[GitInventoryLoadError] = []
    sites: dict[str, LoadedGitSite] = {}
    devices: list[GitInventoryDeviceV1] = []
    device_files: dict[str, str] = {}

    for directory, label in ((devices_dir, "devices"), (sites_dir, "sites")):
        if directory.is_symlink():
            errors.append(
                GitInventoryLoadError(
                    f"{inventory_path}/{label}", "Symlinked directories are not allowed"
                )
            )
        elif directory.exists() and not directory.is_dir():
            errors.append(
                GitInventoryLoadError(
                    f"{inventory_path}/{label}", "Inventory path is not a directory"
                )
            )
        elif directory.exists():
            resolved_directory = directory.resolve()
            if (
                root_resolved not in resolved_directory.parents
                and resolved_directory != root_resolved
            ):
                errors.append(
                    GitInventoryLoadError(
                        f"{inventory_path}/{label}", "Inventory path escapes repository"
                    )
                )
    if errors:
        return LoadedGitInventory([], {}, {}, errors)

    device_inventory_files = sorted(devices_dir.glob("*.yaml")) if devices_dir.exists() else []
    device_inventory_files.extend(sorted(devices_dir.glob("*.yml")) if devices_dir.exists() else [])
    site_inventory_files, site_scan_errors = _collect_site_inventory_files(
        sites_dir,
        root_resolved=root_resolved,
    )
    if site_scan_errors:
        return LoadedGitInventory([], {}, {}, site_scan_errors)
    if len(device_inventory_files) + len(site_inventory_files) > max_files:
        return LoadedGitInventory(
            [],
            {},
            {},
            [GitInventoryLoadError(inventory_path, "Inventory file count limit exceeded")],
        )

    for path in site_inventory_files:
        rel = path.relative_to(root_resolved).as_posix()
        try:
            _validate_file(path, max_file_bytes=max_file_bytes)
            payload = _load_yaml(path)
            site = GitInventorySiteV1.model_validate(payload)
            if site.external_id != path.stem:
                raise ValueError("Site external_id must match file stem")
            site_external_id = _derive_site_external_id(path, sites_dir)
            if site_external_id in sites:
                raise ValueError(f"Duplicate site external_id: {site_external_id}")
            sites[site_external_id] = LoadedGitSite(
                external_id=site_external_id,
                parent_external_id=_parent_site_external_id(site_external_id),
                model=site,
                file_path=rel,
            )
        except ValidationError as exc:
            errors.append(GitInventoryLoadError(rel, _validation_message(exc)))
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(GitInventoryLoadError(rel, str(exc)))

    errors.extend(_validate_site_hierarchy(sites))

    seen_external_ids: set[str] = set()
    for path in device_inventory_files:
        rel = path.relative_to(root_resolved).as_posix()
        try:
            _validate_file(path, max_file_bytes=max_file_bytes)
            payload = _load_yaml(path)
            device = GitInventoryDeviceV1.model_validate(payload)
            if device.external_id in seen_external_ids:
                raise ValueError(f"Duplicate device external_id: {device.external_id}")
            if path.stem != device.external_id:
                raise ValueError("Device external_id must match file stem")
            if device.site and device.site not in sites:
                raise ValueError(f"Unknown site: {device.site}")
            seen_external_ids.add(device.external_id)
            devices.append(device)
            device_files[device.external_id] = rel
        except ValidationError as exc:
            errors.append(GitInventoryLoadError(rel, _validation_message(exc)))
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(GitInventoryLoadError(rel, str(exc)))

    if errors:
        return LoadedGitInventory([], sites, {}, errors)
    return LoadedGitInventory(devices, sites, device_files, [])


def _validate_site_hierarchy(sites: dict[str, LoadedGitSite]) -> list[GitInventoryLoadError]:
    errors: list[GitInventoryLoadError] = []

    for site in sites.values():
        if site.parent_external_id is not None and site.parent_external_id not in sites:
            errors.append(
                GitInventoryLoadError(
                    site.file_path,
                    f"Unknown parent site: {site.parent_external_id}",
                )
            )

    return errors


def _collect_site_inventory_files(
    sites_dir: Path, *, root_resolved: Path
) -> tuple[list[Path], list[GitInventoryLoadError]]:
    if not sites_dir.exists():
        return [], []

    files: list[Path] = []
    errors: list[GitInventoryLoadError] = []
    stack = [sites_dir]

    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir(), key=lambda path: path.name):
            rel = child.relative_to(root_resolved).as_posix()
            if child.is_symlink():
                errors.append(
                    GitInventoryLoadError(
                        rel,
                        (
                            "Symlinked directories are not allowed"
                            if child.is_dir()
                            else "Symlinks are not allowed"
                        ),
                    )
                )
                continue

            if child.is_dir():
                resolved_child = child.resolve()
                if root_resolved not in resolved_child.parents and resolved_child != root_resolved:
                    errors.append(GitInventoryLoadError(rel, "Inventory path escapes repository"))
                    continue
                stack.append(child)
                continue

            if child.suffix.lower() in {".yaml", ".yml"}:
                files.append(child)

    files.sort(key=lambda path: path.relative_to(sites_dir).as_posix())
    return files, errors


def _derive_site_external_id(path: Path, sites_dir: Path) -> str:
    parts = list(path.relative_to(sites_dir).with_suffix("").parts)
    if len(parts) >= 2 and parts[-1] == parts[-2]:
        parts = parts[:-1]
    return "/".join(parts)


def _parent_site_external_id(site_external_id: str) -> str | None:
    if "/" not in site_external_id:
        return None
    return site_external_id.rsplit("/", 1)[0]


def _validation_message(exc: ValidationError) -> str:
    """A schema failure as text that never carries the offending input.

    ``str(exc)`` renders every error with ``input_value=...``, so a mistyped key holding
    a literal credential would travel into the error envelope and from there to the UI
    and the logs. The message is built from each error's location and type only;
    ``include_url`` strips nothing but documentation links, and the pydantic ``msg`` is
    left out because a validator's own wording is free to quote what it was given.
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(item) for item in error.get("loc", ())) or "document"
        parts.append(f"{location} ({error.get('type', 'invalid')})")
    return "Schema validation failed: " + "; ".join(parts)


def _validate_file(path: Path, *, max_file_bytes: int) -> None:
    if path.is_symlink():
        raise ValueError("Symlinks are not allowed")
    if path.stat().st_size > max_file_bytes:
        raise ValueError("Inventory file size limit exceeded")


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        # DuplicateKeyLoader subclasses yaml.SafeLoader (no arbitrary object
        # instantiation); a custom loader is required because yaml.safe_load()
        # cannot take a Loader= and we need duplicate-key rejection.
        return yaml.load(fh, Loader=DuplicateKeyLoader)  # nosec B506 - SafeLoader subclass
