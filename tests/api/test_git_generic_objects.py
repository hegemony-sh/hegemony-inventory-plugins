# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic Git families exercise the SDK without any NetBox catalogue."""

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from hegemony_inventory_git.loader import load_git_inventory
from hegemony_inventory_git.provider import GitInventoryProvider
from hegemony_inventory_sdk import InventoryProviderError, ProviderCallContext
from tests.inventory_fakes import FakeGitFetcher, FakePlatformServices


def write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload))


def definition(kind="example.service"):
    return {
        "schema_version": 1,
        "kind": "object_type",
        "id": kind,
        "display_name": "Service",
        "field_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scope", "port"],
            "properties": {
                "scope": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "enabled": {"type": "boolean"},
                "owner": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "details": {
                    "type": "object",
                    "properties": {"owner": {"type": ["string", "null"]}},
                },
            },
        },
        "filters": ["scope", "port", "enabled", "owner"],
        "ui": {
            "columns": ["scope", "port"],
            "search_fields": ["owner"],
            "default_sort": "port",
        },
    }


def record(identity="portal", kind="example.service", **attributes):
    return {
        "schema_version": 1,
        "kind": "object",
        "object_type": kind,
        "external_id": identity,
        "name": identity,
        "attributes": {"scope": "east", "port": 443, **attributes},
    }


def setup_inventory(tmp_path):
    write(tmp_path / "inventory/object-types/service.yaml", definition())
    write(
        tmp_path / "inventory/objects/example.service/portal.yaml",
        record(enabled=True, owner="Ops", details={"owner": "Ops"}),
    )
    return tmp_path


def provider(root):
    return GitInventoryProvider(
        provider_id="git:example",
        config={"git_repo_id": str(uuid4()), "path": "inventory"},
        services=FakePlatformServices(git=FakeGitFetcher(path=str(root))),
    )


def ctx(operation="test"):
    return ProviderCallContext(provider_id="git:example", operation=operation)


def test_declarative_attributes_are_typed_and_not_coerced(tmp_path):
    setup_inventory(tmp_path)
    loaded = load_git_inventory(tmp_path, "inventory")
    assert not loaded.errors
    assert loaded.generic.records["example.service"]["portal"].attributes["port"] == 443
    write(tmp_path / "inventory/objects/example.service/portal.yaml", record(port="443"))
    assert "attributes.port" in load_git_inventory(tmp_path, "inventory").errors[0].message


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=True),
        lambda d: d.update(id="site"),
        lambda d: d["field_schema"].update({"$ref": "https://example.org/schema"}),
        lambda d: d["field_schema"]["properties"]["port"].update(pattern=".*"),
        lambda d: d.update(filters=["unknown"]),
        lambda d: d.update(ui={"custom_component": "arbitrary"}),
        lambda d: d.update(ui={"columns": ["unknown"]}),
    ],
)
def test_bad_definitions_fail_closed(tmp_path, mutate):
    setup_inventory(tmp_path)
    data = definition()
    mutate(data)
    write(tmp_path / "inventory/object-types/service.yaml", data)
    loaded = load_git_inventory(tmp_path, "inventory")
    assert loaded.errors and not loaded.generic.specs


def test_relationships_validate_after_all_records_load(tmp_path):
    setup_inventory(tmp_path)
    data = definition()
    data["relationships"] = [
        {"rel": "scope", "target_object_type": "example.scope", "required": True}
    ]
    write(tmp_path / "inventory/object-types/service.yaml", data)
    write(tmp_path / "inventory/object-types/scope.yaml", definition("example.scope"))
    write(tmp_path / "inventory/objects/example.scope/east.yaml", record("east", "example.scope"))
    obj = record()
    obj["relationships"] = [
        {"rel": "scope", "target_object_type": "example.scope", "target_external_id": "east"}
    ]
    write(tmp_path / "inventory/objects/example.service/portal.yaml", obj)
    assert not load_git_inventory(tmp_path, "inventory").errors
    obj["relationships"][0]["target_external_id"] = "missing"
    write(tmp_path / "inventory/objects/example.service/portal.yaml", obj)
    assert "relationships" in load_git_inventory(tmp_path, "inventory").errors[0].message


def test_duplicates_limits_symlinks_and_empty_types(tmp_path):
    setup_inventory(tmp_path)
    write(tmp_path / "inventory/objects/example.service/duplicate.yaml", record())
    assert "duplicate ID" in load_git_inventory(tmp_path, "inventory").errors[0].message
    (tmp_path / "inventory/objects/example.service/duplicate.yaml").unlink()
    assert load_git_inventory(tmp_path, "inventory", max_files=1).errors
    assert load_git_inventory(tmp_path, "inventory", max_file_bytes=20).errors
    (tmp_path / "inventory/objects/example.service/portal.yaml").unlink()
    assert not load_git_inventory(tmp_path, "inventory").errors
    (tmp_path / "inventory/objects/example.service/escape.yaml").symlink_to(tmp_path / "outside")
    assert "symlink" in load_git_inventory(tmp_path, "inventory").errors[0].message


@pytest.mark.asyncio
async def test_discovery_reads_filters_and_errors(tmp_path):
    p = provider(setup_inventory(tmp_path))
    discovery = await p.discover(context=ctx())
    assert set(discovery.mappings) == {"example.service"}
    assert discovery.bindings == ()
    assert (await p.list_objects("example.service", {"q": "ops"}, context=ctx()))[
        0
    ].external_id == "portal"
    assert not await p.list_objects("example.service", {"attr.scope": "west"}, context=ctx())
    assert not await p.list_objects("example.service", limit=0, context=ctx())
    for query in ({"unknown": 1}, {"attr.port": "443"}, {"attr.enabled": 1}):
        with pytest.raises(InventoryProviderError):
            await p.list_objects("example.service", query, context=ctx())
    with pytest.raises(InventoryProviderError):
        await p.get_object("example.service", "absent", context=ctx())
    with pytest.raises(InventoryProviderError):
        await p.list_objects("missing", context=ctx())


@pytest.mark.asyncio
async def test_complete_paging_and_snapshot_isolation(tmp_path):
    setup_inventory(tmp_path)
    for n in range(501):
        write(tmp_path / f"inventory/objects/example.service/{n}.yaml", record(str(n), port=n + 1))
    p = provider(tmp_path)
    await p.discover(context=ctx("discover_cached"))
    write(tmp_path / "inventory/objects/example.service/portal.yaml", record(port="invalid"))
    pages = [
        page
        async for page in p.iter_object_pages(
            "example.service", context=ctx("auto_sync_objects:example.service")
        )
    ]
    assert [len(page) for page in pages] == [500, 2]
    assert len({obj.external_id for page in pages for obj in page}) == 502
    with pytest.raises(InventoryProviderError):
        await p.discover(context=ctx("discover_cached"))
    write(tmp_path / "inventory/objects/example.service/portal.yaml", record(port=8443))
    await p.discover(context=ctx("discover_cached"))
    assert (await p.get_object("example.service", "portal", context=ctx("auto_sync"))).attributes[
        "port"
    ] == 8443


@pytest.mark.asyncio
async def test_file_rename_and_copy_do_not_change_identity_or_mutate_snapshot(tmp_path):
    setup_inventory(tmp_path)
    p = provider(tmp_path)
    await p.discover(context=ctx("discover_cached"))
    first = await p.get_object("example.service", "portal", context=ctx("auto_sync"))
    first.attributes["details"]["owner"] = "changed"
    assert (await p.get_object("example.service", "portal", context=ctx("auto_sync"))).attributes[
        "details"
    ]["owner"] == "Ops"
    (tmp_path / "inventory/objects/example.service/portal.yaml").rename(
        tmp_path / "inventory/objects/example.service/renamed.yaml"
    )
    assert (await p.get_object("example.service", "portal", context=ctx())).external_id == "portal"


def test_documented_generic_repository_loads_without_special_cases():
    root = Path(__file__).resolve().parents[2] / "plugins/inventory_git/examples/generic-objects"
    loaded = load_git_inventory(root, "inventory")
    assert not loaded.errors
    assert set(loaded.generic.specs) == {
        "example.scope",
        "example.service",
        "example.vlan",
        "example.prefix",
    }
    assert loaded.generic.records["example.service"]["portal"].attributes["settings"] == {
        "tier": "production"
    }


@pytest.mark.asyncio
async def test_old_host_remains_legacy_only(tmp_path):
    class OldFetcher:
        async def get_workdir(self, **kwargs):
            from hegemony_inventory_sdk import GitWorkdir

            return GitWorkdir(path=str(tmp_path), head_sha="a" * 40)

    (tmp_path / "inventory").mkdir()
    p = provider(tmp_path)
    p._services.git = OldFetcher()
    assert (await p.discover(context=ctx())).object_types == ()
    setup_inventory(tmp_path)
    with pytest.raises(InventoryProviderError, match="read-lock support"):
        await p.discover(context=ctx())


@pytest.mark.parametrize(
    "payload",
    [
        "a: &loop [*loop]",
        "schema_version: 1\nschema_version: 1",
        "value: .inf",
    ],
)
def test_hostile_yaml_fails_closed(tmp_path, payload):
    setup_inventory(tmp_path)
    (tmp_path / "inventory/objects/example.service/portal.yaml").write_text(payload)
    assert load_git_inventory(tmp_path, "inventory").errors


def test_yaml_parse_error_never_exposes_record_values(tmp_path):
    setup_inventory(tmp_path)
    path = tmp_path / "inventory/objects/example.service/portal.yaml"
    path.write_text('attributes: [secret-password: "SENSITIVE_VALUE",\n')
    errors = load_git_inventory(tmp_path, "inventory").errors
    assert errors and "portal.yaml" in errors[0].message
    assert "SENSITIVE_VALUE" not in str(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scalar", ["!!int SENSITIVE", "!!timestamp SENSITIVE", "!!float SENSITIVE"]
)
async def test_yaml_constructor_errors_are_safe(tmp_path, scalar):
    setup_inventory(tmp_path)
    path = tmp_path / "inventory/objects/example.service/portal.yaml"
    path.write_text(path.read_text() + f"bad: {scalar}\n")
    with pytest.raises(InventoryProviderError) as raised:
        await provider(tmp_path).discover(context=ctx())
    assert "SENSITIVE" not in str(raised.value.envelope.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("descending", [False, True])
async def test_mixed_numbers_sort_numerically_before_limit(tmp_path, descending):
    data = definition()
    data["field_schema"]["properties"]["port"] = {"type": "number"}
    data["ui"]["default_sort"] = "-port" if descending else "port"
    write(tmp_path / "inventory/object-types/service.yaml", data)
    for n, value in enumerate([2, 100.5, 1.5, 100]):
        write(tmp_path / f"inventory/objects/example.service/{n}.yaml", record(str(n), port=value))
    objects = await provider(tmp_path).list_objects("example.service", limit=2, context=ctx())
    assert [o.attributes["port"] for o in objects] == ([100.5, 100] if descending else [1.5, 2])


@pytest.mark.asyncio
async def test_cached_lookup_does_not_serialize_records(tmp_path, monkeypatch):
    from hegemony_inventory_git.generic import Record

    setup_inventory(tmp_path)
    p = provider(tmp_path)
    await p.discover(context=ctx("discover_cached"))

    def unexpected_dump(*args, **kwargs):
        pytest.fail("lookup must not serialize complete record payloads")

    monkeypatch.setattr(Record, "model_dump", unexpected_dump)
    assert (
        await p.get_object("example.service", "portal", context=ctx("auto_sync"))
    ).name == "portal"


@pytest.mark.asyncio
async def test_cached_legacy_descriptors_own_nested_payloads(tmp_path):
    write(
        tmp_path / "inventory/sites/east.yaml",
        {
            "schema_version": 1,
            "kind": "site",
            "external_id": "east",
            "name": "East",
            "custom_fields": {"nested": {"owner": "Ops"}},
        },
    )
    write(
        tmp_path / "inventory/devices/router.yaml",
        {
            "schema_version": 1,
            "kind": "device",
            "external_id": "router",
            "name": "Router",
            "mgmt_host": "192.0.2.1",
            "site": "east",
            "attributes": {"nested": {"owner": "Ops"}},
            "custom_fields": {"nested": {"owner": "Ops"}},
        },
    )
    p = provider(tmp_path)
    await p.discover(context=ctx("discover_cached"))
    first = (await p.query_devices("", context=ctx("auto_sync")))[0]
    first.attributes["nested"]["owner"] = "changed"
    first.descriptive["custom_fields"]["nested"]["owner"] = "changed"
    first.site.descriptive["nested"]["owner"] = "changed"
    site = (await p.list_sites(context=ctx("auto_sync")))[0]
    site.descriptive["nested"]["owner"] = "changed again"
    second = (await p.query_devices("", context=ctx("auto_sync")))[0]
    assert second.attributes["nested"]["owner"] == "Ops"
    assert second.descriptive["custom_fields"]["nested"]["owner"] == "Ops"
    assert second.site.descriptive["nested"]["owner"] == "Ops"
