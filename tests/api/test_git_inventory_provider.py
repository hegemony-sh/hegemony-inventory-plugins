# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git inventory provider config and loader validation."""

import textwrap
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hegemony_inventory_git.config import GitInventoryProviderConfig
from hegemony_inventory_git.inventory_schema import GitInventoryDeviceV1
from hegemony_inventory_git.loader import load_git_inventory
from hegemony_inventory_git.provider import GitInventoryProvider
from hegemony_inventory_git.query import matches_query
from hegemony_inventory_sdk import (
    Capability,
    DeviceDescriptor,
    InventoryProviderError,
    ProviderCallContext,
    ProviderErrorCode,
    ResourceType,
    SiteRef,
)
from tests.inventory_fakes import FakeGitFetcher, FakePlatformServices


def _config(path: str) -> GitInventoryProviderConfig:
    return GitInventoryProviderConfig(git_repo_id=uuid4(), path=path)


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _required_external_id(device: DeviceDescriptor) -> str:
    assert device.external_id is not None
    return device.external_id


def test_git_inventory_path_rejects_absolute_paths() -> None:
    with pytest.raises(ValidationError, match="safe repository-relative path"):
        _config("/inventory")


def test_git_inventory_path_normalizes_trailing_slash() -> None:
    config = _config("inventory/devices/")

    assert config.path == "inventory/devices"


def test_git_inventory_branch_rejects_whitespace_only() -> None:
    with pytest.raises(ValidationError, match="branch is required"):
        GitInventoryProviderConfig(git_repo_id=uuid4(), branch="   ", path="inventory")


def test_git_inventory_query_splits_comma_delimited_names() -> None:
    device = GitInventoryDeviceV1(
        schema_version=1,
        kind="device",
        external_id="edge-01",
        name="edge-01",
        mgmt_host="192.0.2.10",
    )

    assert matches_query(device, {}, "name=core-*, edge-*") is True
    assert matches_query(device, {}, "name=core-*, dist-*") is False


def test_git_inventory_loader_reports_missing_inventory_path(tmp_path: Path) -> None:
    result = load_git_inventory(tmp_path, "inventory")

    assert [error.message for error in result.errors] == ["Inventory path does not exist"]


def test_git_inventory_loader_rejects_symlinked_section_dirs(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    real_devices = tmp_path / "real-devices"
    real_devices.mkdir()
    (inventory / "devices").symlink_to(real_devices, target_is_directory=True)

    result = load_git_inventory(tmp_path, "inventory")

    assert result.devices == []
    assert any(error.file_path == "inventory/devices" for error in result.errors)
    assert any(error.message == "Symlinked directories are not allowed" for error in result.errors)


def test_git_inventory_loader_rejects_unknown_site_reference(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "inventory" / "devices" / "edge-01.yaml",
        """
        schema_version: 1
        kind: device
        external_id: edge-01
        name: edge-01
        mgmt_host: 192.0.2.10
        site: emea/nl/ams01
        """,
    )

    result = load_git_inventory(tmp_path, "inventory")

    assert [error.message for error in result.errors] == ["Unknown site: emea/nl/ams01"]


def test_git_inventory_loader_rejects_unknown_site_parent(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "cz.yaml",
        """
        schema_version: 1
        kind: site
        external_id: cz
        name: Czechia
        """,
    )

    result = load_git_inventory(tmp_path, "inventory")

    assert [error.message for error in result.errors] == ["Unknown parent site: emea"]


@pytest.mark.parametrize(
    ("relative_path", "document"),
    [
        (
            "devices/edge-01.yaml",
            """
            schema_version: 1
            kind: device
            external_id: edge-01
            name: edge-01
            mgmt_host: 192.0.2.10
            ssh_password: {secret}
            vendor: Cisco
            """,
        ),
        (
            "sites/ams01.yaml",
            """
            schema_version: 1
            kind: site
            external_id: ams01
            name: Amsterdam
            api_token: {secret}
            """,
        ),
    ],
)
def test_git_inventory_loader_never_echoes_a_mistyped_secret_into_the_error(
    tmp_path: Path, relative_path: str, document: str
) -> None:
    """A key the schema rejects may hold a literal credential; the error must not carry it.

    pydantic's ``str(exc)`` renders every error with ``input_value=...``, so a stray
    ``ssh_password: hunter2`` in a device file would reach the UI and the logs through
    the load error envelope. The message names the location and the kind of error and
    nothing else -- and the old top-level ``vendor`` key stays rejected.
    """
    secret = "s3cr3t-literal-hunter2"
    _write_yaml(tmp_path / "inventory" / relative_path, document.format(secret=secret))

    result = load_git_inventory(tmp_path, "inventory")

    assert [error.file_path for error in result.errors] == [f"inventory/{relative_path}"]
    message = result.errors[0].message
    assert secret not in message
    assert "input_value" not in message
    assert "extra_forbidden" in message
    for key in ("ssh_password", "vendor") if "device" in relative_path else ("api_token",):
        assert key in message


def test_git_inventory_loader_allows_duplicate_local_site_external_ids_in_different_paths(
    tmp_path: Path,
) -> None:
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "emea.yaml",
        """
        schema_version: 1
        kind: site
        external_id: emea
        name: EMEA
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "dc.yaml",
        """
        schema_version: 1
        kind: site
        external_id: dc
        name: Prague DC
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "amer" / "amer.yaml",
        """
        schema_version: 1
        kind: site
        external_id: amer
        name: Americas
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "amer" / "dc.yaml",
        """
        schema_version: 1
        kind: site
        external_id: dc
        name: Prague DC
        """,
    )

    result = load_git_inventory(tmp_path, "inventory")

    assert result.errors == []
    assert set(result.sites) == {"amer", "amer/dc", "emea", "emea/dc"}
    assert result.sites["emea/dc"].parent_external_id == "emea"
    assert result.sites["amer/dc"].parent_external_id == "amer"


def test_git_inventory_loader_rejects_duplicate_site_paths_from_file_and_directory_forms(
    tmp_path: Path,
) -> None:
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea.yaml",
        """
        schema_version: 1
        kind: site
        external_id: emea
        name: EMEA Flat
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "emea.yaml",
        """
        schema_version: 1
        kind: site
        external_id: emea
        name: EMEA Nested
        """,
    )

    result = load_git_inventory(tmp_path, "inventory")

    assert [error.message for error in result.errors] == ["Duplicate site external_id: emea"]


@pytest.mark.asyncio
async def test_git_provider_reads_tree_from_injected_git_fetcher(tmp_path: Path) -> None:
    """The provider resolves its working tree via the injected GitFetcher, not a DB/transport."""
    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={
            "git_repo_id": str(uuid4()),
            "path": "inventory",
            "default_access_config": {
                "ssh": {
                    "username_ref": "{{ secret('vault://devices/default-ssh-username') }}",
                }
            },
        },
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    # The fake working tree has no inventory/ directory, so the loader fails closed
    # and the provider surfaces a safe schema-validation error.
    with pytest.raises(InventoryProviderError) as exc_info:
        await provider.query_devices(
            "",
            context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
        )

    assert exc_info.value.envelope.code == ProviderErrorCode.SCHEMA_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_git_provider_allows_duplicate_site_names_under_different_paths(
    tmp_path: Path,
) -> None:
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "emea.yaml",
        """
        schema_version: 1
        kind: site
        external_id: emea
        name: EMEA
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "dc.yaml",
        """
        schema_version: 1
        kind: site
        external_id: dc
        name: Prague DC
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "amer" / "amer.yaml",
        """
        schema_version: 1
        kind: site
        external_id: amer
        name: AMER
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "amer" / "dc.yaml",
        """
        schema_version: 1
        kind: site
        external_id: dc
        name: Prague DC
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "edge-emea.yaml",
        """
        schema_version: 1
        kind: device
        external_id: edge-emea
        name: edge-emea
        mgmt_host: 192.0.2.11
        site: emea/dc
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "edge-amer.yaml",
        """
        schema_version: 1
        kind: device
        external_id: edge-amer
        name: edge-amer
        mgmt_host: 192.0.2.12
        site: amer/dc
        """,
    )

    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={"git_repo_id": str(uuid4()), "path": "inventory"},
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    emea_devices = await provider.query_devices(
        "site=emea/dc",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )
    amer_devices = await provider.query_devices(
        "site=amer/dc",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )
    named_devices = await provider.query_devices(
        "site=Prague DC",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )

    assert [_required_external_id(device) for device in emea_devices] == ["edge-emea"]
    assert [_required_external_id(device) for device in amer_devices] == ["edge-amer"]
    assert sorted(_required_external_id(device) for device in named_devices) == [
        "edge-amer",
        "edge-emea",
    ]


@pytest.mark.asyncio
async def test_git_provider_reads_sites_directory_and_site_hierarchy(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "emea.yaml",
        """
        schema_version: 1
        kind: site
        external_id: emea
        name: EMEA
        location: Europe
        tags:
          region: emea
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "nl" / "nl.yaml",
        """
        schema_version: 1
        kind: site
        external_id: nl
        name: Netherlands
        location: Netherlands
        tags:
          country: nl
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "sites" / "emea" / "nl" / "ams01.yaml",
        """
        schema_version: 1
        kind: site
        external_id: ams01
        name: Amsterdam DC
        location: Amsterdam
        tags:
          facility: dc
        custom_fields:
          timezone: CET
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "edge-01.yaml",
        """
        schema_version: 1
        kind: device
        external_id: edge-01
        name: edge-01
        mgmt_host: 192.0.2.10
        site: emea/nl/ams01
        tags:
          - edge
          - production
        """,
    )

    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={
            "git_repo_id": str(uuid4()),
            "path": "inventory",
            "default_access_config": {
                "ssh": {
                    "username_ref": "{{ secret('vault://devices/default-ssh-username') }}",
                }
            },
        },
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    sites = await provider.list_sites(
        context=ProviderCallContext(provider_id="git:primary", operation="list_sites")
    )
    site = await provider.get(
        ResourceType.SITE,
        "emea/nl/ams01",
        context=ProviderCallContext(provider_id="git:primary", operation="get"),
    )
    devices = await provider.query_devices(
        "site=emea/nl/ams01",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )

    assert isinstance(site, SiteRef)
    assert [item.external_id for item in sites] == ["emea", "emea/nl", "emea/nl/ams01"]
    assert sites[1].parent_external_id == "emea"
    assert site.parent_external_id == "emea/nl"
    assert site.location == "Amsterdam"
    assert site.tags == {"facility": "dc"}
    assert site.descriptive == {"timezone": "CET"}
    assert len(devices) == 1
    assert devices[0].site is not None
    assert devices[0].site.external_id == "emea/nl/ams01"
    assert devices[0].site.parent_external_id == "emea/nl"
    assert devices[0].native_tags == ("edge", "production")
    assert devices[0].access_config == {
        "ssh": {
            "username_ref": "{{ secret('vault://devices/default-ssh-username') }}",
        }
    }


@pytest.mark.asyncio
async def test_git_provider_test_connection_preserves_safe_details(tmp_path: Path) -> None:
    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={"git_repo_id": str(uuid4()), "path": "inventory"},
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    result = await provider.test_connection(
        context=ProviderCallContext(provider_id="git:primary", operation="test_connection")
    )

    assert result.ok is False
    assert result.message == "Git inventory validation failed"
    assert result.error is not None
    assert result.error["code"] == ProviderErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert result.safe_details == {
        "errors": [{"file_path": "inventory", "message": "Inventory path does not exist"}]
    }


@pytest.mark.asyncio
async def test_git_provider_merges_default_jump_host_onto_devices(tmp_path: Path) -> None:
    """A shared bastion hop in default_access_config lands on every device.

    The jump_host scope (platform design: dind-sandbox §11.2) merges exactly
    like ssh/enable, so a lab provider can stamp one bastion on its whole
    fleet while the per-device files stay credential-free.
    """
    _write_yaml(
        tmp_path / "inventory" / "sites" / "lab" / "lab.yaml",
        """
        schema_version: 1
        kind: site
        external_id: lab
        name: Lab
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "r1.yaml",
        """
        schema_version: 1
        kind: device
        external_id: r1
        name: r1
        mgmt_host: 172.20.30.11
        site: lab
        """,
    )

    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={
            "git_repo_id": str(uuid4()),
            "path": "inventory",
            "default_access_config": {
                "ssh": {"username_ref": "{{ secret('vault://lab/ssh/username') }}"},
                "jump_host": {
                    "host": "dind",
                    "port": 2222,
                    "username_ref": "{{ secret('vault://lab/bastion/username') }}",
                    "password_ref": "{{ secret('vault://lab/bastion/password') }}",
                },
            },
        },
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    devices = await provider.query_devices(
        "site=lab",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )

    assert len(devices) == 1
    assert devices[0].access_config["jump_host"] == {
        "host": "dind",
        "port": 2222,
        "username_ref": "{{ secret('vault://lab/bastion/username') }}",
        "password_ref": "{{ secret('vault://lab/bastion/password') }}",
    }


@pytest.mark.asyncio
async def test_git_provider_device_can_override_the_default_jump_host(tmp_path: Path) -> None:
    """A device file may name its own bastion, like ssh/enable can be overridden.

    Regression: the inventory YAML schema forbids unknown keys, so until
    ``jump_host`` was declared on GitInventoryAccessConfigV1 a device file
    carrying one failed validation outright — the provider merged a scope no
    device could ever set, which made jump_host behave unlike every other
    access-config scope for git inventories.
    """
    _write_yaml(
        tmp_path / "inventory" / "sites" / "lab" / "lab.yaml",
        """
        schema_version: 1
        kind: site
        external_id: lab
        name: Lab
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "r1.yaml",
        """
        schema_version: 1
        kind: device
        external_id: r1
        name: r1
        mgmt_host: 172.20.30.11
        site: lab
        """,
    )
    _write_yaml(
        tmp_path / "inventory" / "devices" / "r2.yaml",
        """
        schema_version: 1
        kind: device
        external_id: r2
        name: r2
        mgmt_host: 172.21.30.11
        site: lab
        access_config:
          jump_host:
            host: bastion-b
            port: 2022
        """,
    )

    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={
            "git_repo_id": str(uuid4()),
            "path": "inventory",
            "default_access_config": {
                "jump_host": {
                    "host": "dind",
                    "port": 2222,
                    "username_ref": "{{ secret('vault://lab/bastion/username') }}",
                },
            },
        },
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )

    devices = await provider.query_devices(
        "site=lab",
        context=ProviderCallContext(provider_id="git:primary", operation="query_devices"),
    )
    by_id = {device.external_id: device for device in devices}

    # Inherits the fleet default untouched...
    assert by_id["r1"].access_config["jump_host"] == {
        "host": "dind",
        "port": 2222,
        "username_ref": "{{ secret('vault://lab/bastion/username') }}",
    }
    # ...while the device behind another bastion overrides only what it names,
    # keeping the shared credential ref. An omitted field must not be merged
    # as an explicit null and blank the default.
    assert by_id["r2"].access_config["jump_host"] == {
        "host": "bastion-b",
        "port": 2022,
        "username_ref": "{{ secret('vault://lab/bastion/username') }}",
    }


@pytest.mark.asyncio
async def test_a_provider_that_does_not_discover_says_so(tmp_path: Path) -> None:
    """Discovery is opt-in, and a source of truth with a fixed shape opts out.

    A Git inventory serves exactly the object types this plugin was written for -- there
    is no instance-specific answer to give -- so it neither declares the capability nor
    implements the method, and the SDK's default says exactly that.
    """
    provider = GitInventoryProvider(
        provider_id="git:primary",
        config={"git_repo_id": str(uuid4()), "path": "inventory"},
        services=FakePlatformServices(
            provider_id="git:primary",
            provider_type="git",
            git=FakeGitFetcher(path=str(tmp_path)),
        ),
    )
    assert Capability.DISCOVER not in provider.capabilities
    with pytest.raises(InventoryProviderError) as raised:
        await provider.discover(
            context=ProviderCallContext(provider_id=provider.id, operation="discover")
        )
    assert raised.value.envelope.code == ProviderErrorCode.MALFORMED_QUERY
    assert raised.value.envelope.retryable is False
    assert "discover" in raised.value.envelope.message


def test_device_facts_flow_through_attributes_only():
    from hegemony_inventory_sdk import device_to_object_descriptor

    facts = {"vendor": "Cisco", "model": "C8300", "current_version": "17.12.4", "critical": True}
    payload = {
        "schema_version": 1,
        "kind": "device",
        "external_id": "edge",
        "name": "edge",
        "mgmt_host": "192.0.2.1",
        "attributes": facts,
    }
    device = GitInventoryDeviceV1.model_validate(payload)
    provider = GitInventoryProvider(
        provider_id="git:example",
        config=_config("inventory").model_dump(mode="json"),
        services=FakePlatformServices(),
    )
    descriptor = provider._map_device(device, {}, None, "head")
    assert descriptor.attributes == facts
    stored = device_to_object_descriptor(descriptor)
    for key, value in facts.items():
        assert stored.attributes[key] == value
    for key in ("vendor", "model", "current_version"):
        assert not hasattr(descriptor, key)
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            GitInventoryDeviceV1.model_validate({**payload, key: "old"})
