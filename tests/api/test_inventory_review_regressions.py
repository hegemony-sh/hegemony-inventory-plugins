# SPDX-FileCopyrightText: 2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import pathlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hegemony_inventory_git.provider import GitInventoryProvider
from hegemony_inventory_netbox.config import NetBoxProviderConfig
from hegemony_inventory_netbox.derive import derive_catalogue, registered_catalogue
from hegemony_inventory_netbox.provider import NetBoxInventoryProvider
from hegemony_inventory_sdk import InventoryLimits, ProviderCallContext
from tests.inventory_fakes import FakeGitFetcher, FakePlatformServices


@pytest.mark.asyncio
async def test_git_full_listing_ignores_run_and_preview_caps(tmp_path):
    for i in (1, 2):
        for kind in ("device", "site"):
            folder = tmp_path / "inventory" / f"{kind}s"
            folder.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "kind": kind,
                "external_id": f"{kind}{i}",
                "name": f"{kind}{i}",
            }
            if kind == "device":
                payload["mgmt_host"] = f"192.0.2.{i}"
            (folder / f"{kind}{i}.yaml").write_text(json.dumps(payload))
    p = GitInventoryProvider(
        provider_id="git:review",
        config={"git_repo_id": str(uuid4()), "path": "inventory"},
        services=FakePlatformServices(
            git=FakeGitFetcher(path=str(tmp_path)),
            limits=InventoryLimits(
                max_run_targets=1, max_preview_devices=1, max_provider_pages=100
            ),
        ),
    )
    ctx = ProviderCallContext(provider_id=p.id, operation="auto_sync")
    devices = await p.query_devices("", context=ctx)
    sites = await p.list_sites(context=ctx)
    assert (len(devices), len(sites)) == (2, 2)


def test_discovered_tenant_field_does_not_invent_a_tenant_filter():
    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "netbox-4.3-schema.json"
    schema = json.loads(fixture.read_text())
    schema["paths"]["/api/plugins/review/widgets/"] = {
        "get": {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PaginatedReviewWidgetList"}
                        }
                    }
                }
            }
        }
    }
    schema["components"]["schemas"]["PaginatedReviewWidgetList"] = {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"$ref": "#/components/schemas/ReviewWidget"}}
        },
    }
    schema["components"]["schemas"]["ReviewWidget"] = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "tenant": {"$ref": "#/components/schemas/BriefTenant"},
        },
    }
    entry = next(
        x for x in registered_catalogue(derive_catalogue(schema)) if x.id == "review.widgets"
    )
    p = NetBoxInventoryProvider(
        provider_id="netbox:review",
        services=FakePlatformServices(),
        config={
            "url": "https://netbox.example",
            "token_ref": "{{ env('NETBOX_TOKEN') }}",
            "inventory_scopes": [{"name": "A", "tenant_slug": "tenant-a"}],
            "active_inventory_scope": "A",
        },
    )
    ctx = ProviderCallContext(
        provider_id=p.id,
        operation="review",
        object_mapping=entry.mapping,
        object_query_schema=entry.spec.query_schema,
    )
    plan = p._fetch_plan(entry.id, ctx)
    assert plan is not None
    assert not p._tenant_scope_allows(plan), entry.spec.query_schema


def test_a_raw_token_literal_is_rejected_at_validation():
    with pytest.raises(ValidationError):
        NetBoxProviderConfig.model_validate(
            {"url": "https://netbox.example", "token_ref": "raw-token-example"}
        )


def test_safe_config_exports_a_valid_reference_and_never_a_literal():
    reference = "{{ secret('vault://netbox/token') }}"
    config = NetBoxProviderConfig.model_validate(
        {"url": "https://netbox.example", "token_ref": reference}
    )

    exported = config.safe_config()

    assert exported["token_ref"] == reference
    assert "raw-token-example" not in json.dumps(exported)


def test_scope_rejects_an_unadvertised_tenant_lookup():
    p = NetBoxInventoryProvider(
        provider_id="netbox:review",
        services=FakePlatformServices(),
        config={
            "url": "https://netbox.example",
            "token_ref": "{{ env('NETBOX_TOKEN') }}",
            "inventory_scopes": [
                {
                    "name": "scope",
                    "queries": {
                        "device": [{"field": "tenant", "lookup": "contains", "value": "tenant-a"}]
                    },
                }
            ],
            "active_inventory_scope": "scope",
        },
    )
    ctx = ProviderCallContext(
        provider_id=p.id,
        operation="review",
        object_query_schema={"type": "object", "properties": {"tenant": {"type": "string"}}},
    )
    plan = p._fetch_plan("device", ctx)
    assert plan is not None
    with pytest.raises(ValueError, match="does not advertise"):
        p._object_path(plan, "")


@pytest.mark.parametrize(
    "baseline,caller,expected",
    [
        ("tenant=a&tenant=b", "tenant=b&tenant=c", "tenant=b"),
        ("tag=managed", "tag=core", "tag=managed&tag=core"),
        ("status=active&status=planned", "", "status=active&status=planned"),
        # A positive lookup still narrows: what both allow is the common value.
        ("status=active&status=planned", "status=planned", "status=planned"),
        # Negated lookups: NetBox excludes every listed value, so requiring both
        # exclusions is the union. Intersecting these re-admitted planned devices.
        (
            "status__n=offline&status__n=planned",
            "status__n=offline",
            "status__n=offline&status__n=planned",
        ),
        ("status__n=offline", "status__n=planned", "status__n=offline&status__n=planned"),
        ("name__nic=lab", "name__nic=test", "name__nic=lab&name__nic=test"),
        ("tag__n=lab", "tag__n=test", "tag__n=lab&tag__n=test"),
        # Bounds: repeated values OR, so each side is its loosest; both require the tighter.
        ("id__gte=5", "id__gte=10", "id__gte=10"),
        ("id__gte=5&id__gte=10", "id__gte=7", "id__gte=7"),
        ("id__lte=20", "id__lte=30", "id__lte=20"),
        ("id__lt=20", "id__lt=15", "id__lt=15"),
        # Strict and inclusive are distinct parameters, which NetBox already ANDs.
        ("id__gt=5", "id__gte=5", "id__gt=5&id__gte=5"),
        (
            "last_updated__gte=2026-09-15T12:00:00Z",
            "last_updated__gte=2026-09-15T11:00:00.000000Z",
            "last_updated__gte=2026-09-15T12%3A00%3A00Z",
        ),
        # Pattern lookups: expressible only when one side is a subset of the other.
        ("name__ic=core", "name__ic=core", "name__ic=core"),
        ("name__ic=core&name__ic=edge", "name__ic=core", "name__ic=core"),
        # tag__any is NetBox's OR spelling of the tag filter, so it intersects.
        ("tag__any=a&tag__any=b", "tag__any=b", "tag__any=b"),
    ],
)
def test_independent_scope_constraints_intersect(baseline, caller, expected):
    from hegemony_inventory_netbox.provider import _intersect_queries

    assert _intersect_queries(baseline, caller) == expected


@pytest.mark.parametrize(
    "baseline,caller,message",
    [
        ("status=active", "status=planned", "Conflicting"),
        ("tag__any=a", "tag__any=b", "Conflicting"),
        ("name__ic=core", "name__ic=edge", "cannot require two independent name__ic"),
        ("name__isw=core&name__isw=edge", "name__isw=core&name__isw=dist", "cannot require"),
        ("id__gte=5", "id__gte=five", "cannot be compared"),
        ("id__gte=5", "id__gte=2026-01-01", "cannot be compared"),
    ],
)
def test_a_conjunction_netbox_cannot_express_is_refused_rather_than_widened(
    baseline, caller, message
):
    from hegemony_inventory_netbox.provider import _intersect_queries

    with pytest.raises(ValueError, match=message):
        _intersect_queries(baseline, caller)


def test_legacy_migration_conjoins_by_lookup_family_rather_than_intersecting():
    """The migration folds raw ``device_query`` and ``object_queries`` through the same
    table, so a negated exclusion accumulates and a bound tightens instead of widening."""
    config = NetBoxProviderConfig.model_validate(
        {
            "url": "https://netbox.example",
            "token_ref": "{{ env('NETBOX_TOKEN') }}",
            "device_query": "status__n=offline&status__n=planned&id__gte=5",
            "object_queries": {"device": "status__n=offline&id__gte=10&role=router"},
            "inventory_scopes": [
                {
                    "name": "scope",
                    "queries": {
                        "device": [
                            {"field": "status", "lookup": "not", "value": "staged"},
                            {"field": "role", "lookup": "exact", "value": ["router", "switch"]},
                        ]
                    },
                }
            ],
            "active_inventory_scope": "scope",
        }
    )

    rules = {rule.field: rule for rule in config.inventory_filter.queries["device"]}

    assert rules["status__n"].value == ["offline", "planned", "staged"]
    assert rules["id__gte"].value == "10"
    assert rules["role"].value == "router"
    assert {name: rule.schema_validation for name, rule in rules.items()} == {
        "status__n": "legacy",
        "id__gte": "legacy",
        "role": "legacy",
    }


@pytest.mark.asyncio
async def test_request_construction_conjoins_scope_and_caller_by_lookup_family(monkeypatch):
    """The same table governs every read: a caller's negation widens nothing, a caller's
    bound only tightens, and a positive lookup still narrows to the common value."""
    provider = NetBoxInventoryProvider(
        provider_id="netbox:review",
        services=FakePlatformServices(),
        config={
            "url": "https://netbox.example",
            "token_ref": "{{ env('NETBOX_TOKEN') }}",
            "inventory_filter": {
                "queries": {
                    "device": [
                        {"field": "status", "lookup": "not", "value": ["offline", "planned"]},
                        {"field": "id", "lookup": "gte", "value": "5"},
                        {"field": "role", "lookup": "exact", "value": ["router", "switch"]},
                    ]
                }
            },
        },
    )
    schema = {
        "type": "object",
        "properties": {
            "status__n": {"type": "string"},
            "id__gte": {"type": "integer"},
            "role": {"type": "string"},
        },
    }
    ctx = ProviderCallContext(
        provider_id=provider.id, operation="review", object_query_schema=schema
    )
    plan = provider._fetch_plan("device", ctx)
    assert plan is not None

    path = provider._object_path(plan, "status__n=offline&id__gte=10&role=router")

    assert path == ("/api/dcim/devices/?status__n=offline&status__n=planned&id__gte=10&role=router")
    with pytest.raises(ValueError, match="Conflicting"):
        provider._object_path(plan, "role=firewall")


@pytest.mark.parametrize(
    "value", ["plain-token", '{{ "literal-token" }}', "{{ env('KEY') }}suffix"]
)
def test_token_reference_rejects_literal_credentials(value):
    with pytest.raises(ValidationError):
        NetBoxProviderConfig.model_validate({"url": "https://netbox.example", "token_ref": value})


@pytest.mark.parametrize(
    "value",
    ["{{ env('KEY') }}", "{{ file('token') }}", "{{ secret('vault://orgs/default/token/value') }}"],
)
def test_token_reference_preserves_supported_references(value):
    config = NetBoxProviderConfig.model_validate(
        {"url": "https://netbox.example", "token_ref": value}
    )
    assert config.safe_config()["token_ref"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["list_objects", "iter_object_pages", "get_object", "get_site"]
)
@pytest.mark.parametrize(
    "failure", ["undiscovered", "unadvertised", "scope_conflict", "caller_conflict"]
)
async def test_scope_errors_use_the_provider_error_contract(operation, failure, monkeypatch):
    """All public read paths reject invalid scopes before sending an HTTP request.

    A rule the shipped fallback cannot vouch for is a missing discovery, not a malformed
    query, and is reported under the code the other "run Discover" refusal uses.
    """
    from hegemony_inventory_sdk import InventoryProviderError, ProviderErrorCode

    object_type = "site" if operation == "get_site" else "device"
    identity = "slug" if object_type == "site" else "id"
    scope = {"name": "scope"}
    object_queries = {}
    query = None
    query_schema = {}
    expected_code = ProviderErrorCode.MALFORMED_QUERY
    if failure == "undiscovered":
        scope["queries"] = {
            object_type: [{"field": "tenant", "lookup": "contains", "value": "tenant-a"}]
        }
        message = "discovery has not run for this provider"
        expected_code = ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    elif failure == "unadvertised":
        scope["queries"] = {
            object_type: [{"field": "tenant", "lookup": "contains", "value": "tenant-a"}]
        }
        # This instance has described the family, and its filters do not include it.
        query_schema = {"type": "object", "properties": {"tenant": {"type": "string"}}}
        message = "does not advertise filter tenant__ic"
    elif failure == "scope_conflict":
        scope["tenant_slug"] = "tenant-a"
        object_queries[object_type] = "tenant=tenant-b"
        message = "Conflicting"
    else:
        object_queries[object_type] = f"{identity}=2"
        query = {identity: "1"}
        message = "Conflicting"
    provider = NetBoxInventoryProvider(
        provider_id="netbox:review",
        services=FakePlatformServices(),
        config={
            "url": "https://netbox.example",
            "token_ref": "{{ env('NETBOX_TOKEN') }}",
            "inventory_scopes": [scope],
            "active_inventory_scope": "scope",
            "object_queries": object_queries,
        },
    )

    async def unexpected_request(*args, **kwargs):
        raise AssertionError("invalid scope reached HTTP")

    monkeypatch.setattr(provider, "_request_json", unexpected_request)
    context = ProviderCallContext(
        provider_id=provider.id,
        operation=f"review:{operation}",
        object_query_schema=query_schema,
    )
    with pytest.raises(InventoryProviderError, match=message) as caught:
        if operation == "list_objects":
            await provider.list_objects(object_type, query, context=context)
        elif operation == "iter_object_pages":
            async for _ in provider.iter_object_pages(object_type, query, context=context):
                pytest.fail("invalid scope yielded a page")
        else:
            await provider.get_object(object_type, "1", context=context)
    envelope = caught.value.envelope
    assert envelope.code == expected_code
    if failure == "undiscovered":
        assert "Run Discover on this provider first" in envelope.message
    assert envelope.operation == context.operation
    assert envelope.provider_id == provider.id
    assert envelope.provider_type == "netbox"
    assert envelope.retryable is False
    assert isinstance(caught.value.__cause__, ValueError)
