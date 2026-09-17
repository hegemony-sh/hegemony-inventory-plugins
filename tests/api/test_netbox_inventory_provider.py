# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider HTTP semantics."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from hegemony_inventory_netbox import object_types as catalogue, provider as netbox_provider_module
from hegemony_inventory_netbox.config import NetBoxProviderConfig
from hegemony_inventory_netbox.object_types import BY_ID
from hegemony_inventory_netbox.provider import NetBoxInventoryProvider
from hegemony_inventory_sdk import (
    Capability,
    InventoryLimits,
    InventoryProviderError,
    ObjectFieldMapping,
    ProviderCallContext,
    ProviderErrorCode,
    ResourceType,
)
from tests.inventory_fakes import FakePlatformServices


def _provider(
    config: dict[str, Any] | None = None, *, token: str | None = None
) -> NetBoxInventoryProvider:
    provider_config = {
        "url": "https://netbox.example",
        "token_ref": "{{ secret('vault://inventory/netbox/token') }}",
        **(config or {}),
    }
    return NetBoxInventoryProvider(
        provider_id="netbox:primary",
        config=provider_config,
        services=FakePlatformServices(
            token=token, provider_id="netbox:primary", provider_type="netbox"
        ),
    )


def _object_types(provider: NetBoxInventoryProvider) -> set[str]:
    """``supported_resources`` as object-type strings, enum members or not."""
    return {
        resource.value if isinstance(resource, ResourceType) else str(resource)
        for resource in provider.supported_resources
    }


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.MockTransport,
) -> None:
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=handler, **kwargs)

    monkeypatch.setattr(netbox_provider_module.httpx, "AsyncClient", client_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "resolved_token", "expected_header"),
    [
        ({}, "netbox-token", "Bearer netbox-token"),
        ({"auth_scheme": "Token"}, "netbox-token", "Token netbox-token"),
        ({}, "Bearer netbox-token", "Bearer netbox-token"),
        ({"auth_scheme": "Bearer"}, "Token netbox-token", "Token netbox-token"),
    ],
)
async def test_netbox_authorization_header_is_configurable_without_duplicate_prefix(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    resolved_token: str,
    expected_header: str,
) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(config, token=resolved_token)
    await provider.list_objects(
        "interface",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="auth-test",
            query_hash="auth-test",
            config_version=1,
        ),
    )

    assert captured["authorization"] == expected_header


@pytest.mark.asyncio
async def test_netbox_unprefixed_token_retries_token_after_bearer_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization")
        captured.append(authorization)
        if authorization == "Bearer netbox-token":
            return httpx.Response(403, json={"detail": "forbidden"})
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="netbox-token")
    await provider.list_objects(
        "interface",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="read_objects",
            query_hash="auth-fallback-test",
            config_version=1,
        ),
    )

    assert captured == ["Bearer netbox-token", "Token netbox-token"]


@pytest.mark.asyncio
async def test_netbox_client_400_returns_safe_malformed_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "provider payload stays private"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="netbox-token")
    with pytest.raises(InventoryProviderError) as exc_info:
        await provider.list_objects(
            "interface",
            {"bad": "query"},
            limit=10,
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="read_objects",
                query_hash="bad-query",
                config_version=1,
            ),
        )

    envelope = exc_info.value.envelope
    assert envelope.code == ProviderErrorCode.MALFORMED_QUERY
    assert envelope.message == "NetBox rejected the query or request"
    assert envelope.safe_details == {"status_code": 400}
    assert envelope.retryable is False
    assert "provider payload" not in envelope.message


@pytest.mark.asyncio
async def test_netbox_transient_failures_retry_four_times_with_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    client_creations = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        if len(captured) < 4:
            return httpx.Response(500, json={"detail": "temporary"})
        return httpx.Response(200, json={"results": [], "next": None})

    async def no_sleep(_seconds: float) -> None:
        return None

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        nonlocal client_creations
        client_creations += 1
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(netbox_provider_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(netbox_provider_module.asyncio, "sleep", no_sleep)

    provider = _provider(token="netbox-token")
    await provider.list_objects(
        "interface",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="read_objects",
            query_hash="retry-test",
            config_version=1,
        ),
    )

    assert len(captured) == 4
    assert client_creations == 1


@pytest.mark.asyncio
async def test_netbox_rejects_off_origin_pagination_before_sending_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.host or "", request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={"results": [], "next": "https://evil.example/api/dcim/devices/?limit=10"},
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="netbox-token")
    with pytest.raises(InventoryProviderError) as exc_info:
        await provider.list_objects(
            "interface",
            limit=10,
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="read_objects",
                query_hash="pagination-origin-test",
                config_version=1,
            ),
        )

    assert exc_info.value.envelope.code == ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    assert captured == [("netbox.example", "Bearer netbox-token")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": [None], "next": None},
        {"results": [], "next": 42},
    ],
)
async def test_netbox_rejects_malformed_pagination_shapes(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")
    with pytest.raises(InventoryProviderError) as exc_info:
        await provider.list_objects(
            "interface",
            limit=10,
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="read_objects",
            ),
        )

    assert exc_info.value.envelope.code == ProviderErrorCode.PAGINATION_FAILED
    assert exc_info.value.envelope.safe_details["page"] == 1


@pytest.mark.asyncio
async def test_sync_listing_is_not_truncated_by_the_run_target_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_pages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(
            request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        )
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={"results": [{"id": 2, "name": "eth1"}], "next": None},
            )
        return httpx.Response(
            200,
            json={
                "results": [{"id": 1, "name": "eth0"}],
                "next": "https://netbox.example/api/dcim/interfaces/?page=2",
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")
    provider._services.limits = InventoryLimits(
        max_provider_pages=10,
        max_run_targets=1,
        max_preview_devices=1,
    )

    objects = await provider.list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="auto_sync"),
    )

    assert [obj.external_id for obj in objects] == ["1", "2"]
    assert len(requested_pages) == 2


def test_configured_projection_bindings_use_current_provider_settings() -> None:
    provider = _provider(
        {
            "site_tree_source": "site_group",
            "status_map": {"planned": "maintenance"},
            "tag_key_prefix": "nb:",
        }
    )

    bindings = {
        binding.object_type: binding
        for binding in provider.configured_projection_bindings(
            {"device", "site", "region", "site_group"}
        )
    }

    assert set(bindings) == {"device", "site", "site_group"}
    assert bindings["site"].parent_edge == "group"
    assert bindings["device"].maintenance_state_map == {"planned": "maintenance"}
    assert bindings["device"].tag_prefix == "nb:"


def test_netbox_config_normalizes_field_map_entries() -> None:
    config = NetBoxProviderConfig.model_validate(
        {
            "url": "https://netbox.example",
            "token_ref": "vault://token",
            "field_map": {
                " platform ": " platform.slug ",
                "access_config.ssh.username_ref": " custom_fields.hegemony_ssh_username_ref ",
                "": "name",
            },
        }
    )

    assert config.field_map == {
        "access_config.ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
    }


def test_netbox_supports_ipam_object_types() -> None:
    """A provider offers devices, sites and every catalogued family, always."""
    offered = _object_types(_provider())
    assert {"device", "site", "ip_prefix", "ip_address", "vlan"} <= offered


@pytest.mark.asyncio
async def test_netbox_list_objects_maps_ip_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 1,
                        "prefix": "10.0.0.0/24",
                        "status": {"value": "active"},
                        "vlan": {"vid": 10},
                        "role": {"slug": "lan"},
                        "site": {"slug": "ams01"},
                        "is_pool": False,
                        "description": "core",
                    },
                    {
                        "id": 2,
                        "prefix": "10.0.1.0/24",
                        "status": {"value": "reserved"},
                        "vlan": None,
                        "role": None,
                        "site": None,
                        "is_pool": True,
                        "description": "",
                    },
                ],
                "next": None,
            },
        )

    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    objects = await provider.list_objects(
        "ip_prefix",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="list_objects:ip_prefix",
            query_hash="",
            config_version=1,
        ),
    )

    assert captured_paths == ["/api/ipam/prefixes/"]
    assert [o.object_type for o in objects] == ["ip_prefix", "ip_prefix"]
    assert [o.external_id for o in objects] == ["1", "2"]
    assert [o.name for o in objects] == ["10.0.0.0/24", "10.0.1.0/24"]
    assert set(objects[0].attributes) == set(BY_ID["ip_prefix"].attributes)
    expected = {
        "prefix": "10.0.0.0/24",
        "status": "active",
        "vlan": 10,
        "role": "lan",
        # NetBox 4.2 replaced Prefix.site with a generic scope. Both spellings are
        # mapped, so a record from either release resolves and the other reads null.
        "site": "ams01",
        "scope": None,
        "scope_type": None,
        "vrf": None,
        "tenant": None,
        "is_pool": False,
        "description": "core",
    }
    assert {name: objects[0].attributes[name] for name in expected} == expected
    # Absent nested values map to None rather than raising or being stringified.
    assert objects[1].attributes["vlan"] is None
    assert objects[1].attributes["site"] is None


@pytest.mark.asyncio
async def test_netbox_list_objects_maps_the_prefix_scope_on_netbox_4_2_and_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NetBox 4.2 replaced ``Prefix.site`` with a generic scope.

    A prefix from a modern NetBox carries no ``site`` at all, so mapping only that
    key left every synced prefix with a blank site and nothing to say why. The
    scope can also be a region, a site group or a location -- which is why the type
    is carried alongside the name rather than the name being relabelled ``site``.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 3,
                        "prefix": "10.90.1.0/24",
                        "status": {"value": "active"},
                        "vlan": {"vid": 100},
                        "role": {"slug": "management"},
                        "scope_type": "dcim.site",
                        "scope_id": 4,
                        "scope": {"name": "Prague 1", "slug": "prg1"},
                        "tenant": {"slug": "meridian-internal"},
                        "is_pool": False,
                        "description": "PRG1 management",
                    }
                ],
                "next": None,
            },
        )

    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    objects = await provider.list_objects(
        "ip_prefix",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="list_objects:ip_prefix",
            query_hash="",
            config_version=1,
        ),
    )

    attributes = objects[0].attributes
    assert attributes["scope"] == "Prague 1"
    assert attributes["scope_type"] == "dcim.site"
    # The pre-4.2 key is simply absent upstream, and maps to None rather than raising.
    assert attributes["site"] is None
    assert attributes["tenant"] == "meridian-internal"


#: The dotted paths a VLAN record resolves through, and what they should yield. A
#: rendered choice through ``status.value``, three references each read by a different
#: natural key, and one reference the record does not carry at all.
_EXPECTED_VLAN = {
    "vid": 100,
    "name": "prod-web",
    "status": "active",
    "site": "ams01",
    "group": "campus",
    "role": "server",
    "tenant": None,
    "description": "web tier",
}


@pytest.mark.asyncio
async def test_netbox_list_objects_maps_vlans(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ipam/vlans/"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 7,
                        "vid": 100,
                        "name": "prod-web",
                        "status": {"value": "active"},
                        "site": {"slug": "ams01"},
                        "group": {"name": "campus"},
                        "role": {"slug": "server"},
                        "description": "web tier",
                    }
                ],
                "next": None,
            },
        )

    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    objects = await provider.list_objects(
        "vlan",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="list_objects:vlan",
            query_hash="",
            config_version=1,
        ),
    )

    assert len(objects) == 1
    vlan = objects[0]
    assert (vlan.object_type, vlan.external_id, vlan.name) == ("vlan", "7", "prod-web")
    # The mapper emits exactly the attributes the family declares -- no more, so a
    # record cannot smuggle a field past the catalogue, and no fewer, so a path the
    # record does not carry is stored as null rather than silently dropped.
    assert set(vlan.attributes) == set(BY_ID["vlan"].attributes)
    assert {name: vlan.attributes[name] for name in _EXPECTED_VLAN} == _EXPECTED_VLAN


def _json_transport(
    routes: dict[str, Any], recorder: list[str] | None = None
) -> httpx.MockTransport:
    """Serve canned JSON per request path, recording the full URL of each call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(str(request.url))
        payload = routes.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_netbox_get_object_fetches_the_ipam_detail_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPAM detail pages resolve instead of raising "not supported"."""
    provider = _provider(token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/ipam/prefixes/42/": {
                    "id": 42,
                    "prefix": "10.0.0.0/24",
                    "status": {"value": "active"},
                    "site": {"slug": "prg1"},
                }
            },
            seen,
        ),
    )

    descriptor = await provider.get_object(
        "ip_prefix",
        "42",
        context=ProviderCallContext(provider_id="netbox:primary", operation="get_object"),
    )

    assert seen == ["https://netbox.example/api/ipam/prefixes/42/"]
    assert descriptor.object_type == "ip_prefix"
    assert descriptor.external_id == "42"
    assert descriptor.name == "10.0.0.0/24"
    assert descriptor.attributes["status"] == "active"
    assert descriptor.attributes["site"] == "prg1"


@pytest.mark.asyncio
async def test_netbox_get_object_rejects_an_unserved_object_type() -> None:
    """A type the catalogue does not know still fails closed."""
    provider = _provider()

    with pytest.raises(InventoryProviderError) as excinfo:
        await provider.get_object(
            "unicorn",
            "1",
            context=ProviderCallContext(provider_id="netbox:primary", operation="get_object"),
        )

    assert "not supported" in excinfo.value.envelope.message


@pytest.mark.asyncio
async def test_netbox_list_objects_pushes_the_query_to_netbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPAM filtering happens server-side rather than being silently dropped."""
    provider = _provider(token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport({"/api/ipam/vlans/": {"results": [], "next": None}}, seen),
    )

    await provider.list_objects(
        "vlan",
        {"site": "prg1", "status": "active"},
        context=ProviderCallContext(provider_id="netbox:primary", operation="list_objects"),
    )

    assert seen == ["https://netbox.example/api/ipam/vlans/?site=prg1&status=active&limit=100"]


def test_netbox_provider_url_rejects_an_off_origin_link() -> None:
    """A NetBox echoing another host must not become an off-site redirect.

    ``display_url`` is whatever the instance says it is, and it is rendered as a link an
    operator clicks. A NetBox that has been told its own base URL is somewhere else --
    or one behind a proxy that rewrites it -- would otherwise turn every row on the
    objects page into a link off this origin.
    """
    assert (
        _provider()._provider_url(
            {"id": 1, "name": "r1", "display_url": "https://evil.example/dcim/devices/1/"}
        )
        is None
    )


def test_netbox_provider_url_prefers_the_instance_link_on_its_own_origin() -> None:
    """The link is worth having when it is genuinely this NetBox's own."""
    assert (
        _provider()._provider_url(
            {"id": 1, "name": "r1", "display_url": "https://netbox.example/dcim/devices/1/"}
        )
        == "https://netbox.example/dcim/devices/1/"
    )


@pytest.mark.asyncio
async def test_netbox_device_query_narrows_every_device_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-sync calls query_devices with an empty expression.

    Without a configured baseline that pulls the entire NetBox device table.
    """
    provider = _provider({"device_query": "?status=active&role=wan-edge"}, token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport({"/api/dcim/devices/": {"results": [], "next": None}}, seen),
    )
    context = ProviderCallContext(provider_id="netbox:primary", operation="read_objects")

    await provider.list_objects("device", context=context)
    await provider.list_objects("device", {"site": "prg1"}, context=context)

    assert seen == [
        "https://netbox.example/api/dcim/devices/?status=active&role=wan-edge&limit=100",
        "https://netbox.example/api/dcim/devices/?status=active&role=wan-edge&site=prg1&limit=100",
    ]


@pytest.mark.asyncio
async def test_active_saved_scope_maps_a_tenant_and_compiles_typed_family_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One scope applies consistently without widening families that have no tenant."""
    provider = _provider(
        {
            "inventory_scopes": [
                {
                    "name": "Tenant A",
                    "tenant_slug": "tenant-a",
                    "queries": {
                        "device": [{"field": "status", "lookup": "not", "value": "offline"}],
                        "interface": [{"field": "device_id", "lookup": "exact", "value": "7"}],
                    },
                    "tenant_scope_exemptions": ["interface"],
                }
            ],
            "active_inventory_scope": "Tenant A",
        },
        token="netbox-token",
    )
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/devices/": {"results": [], "next": None},
                "/api/dcim/sites/": {"results": [], "next": None},
                "/api/dcim/interfaces/": {"results": [], "next": None},
                "/api/tenancy/tenants/": {"results": [], "next": None},
            },
            seen,
        ),
    )
    context = ProviderCallContext(provider_id="netbox:primary", operation="read_objects")

    await provider.list_objects("device", context=context)
    await provider.list_objects("site", context=context)
    await provider.list_objects("interface", context=context)
    await provider.list_objects("tenant", context=context)

    assert seen == [
        "https://netbox.example/api/dcim/devices/?tenant=tenant-a&status__n=offline&limit=100",
        "https://netbox.example/api/dcim/sites/?tenant=tenant-a&limit=100",
        # Interfaces carry no tenant themselves. The provider never guesses a relational
        # filter; this family is read only because the scope explicitly exempts it.
        "https://netbox.example/api/dcim/interfaces/?device_id=7&limit=100",
        "https://netbox.example/api/tenancy/tenants/?slug=tenant-a&limit=100",
    ]


@pytest.mark.asyncio
async def test_tenant_scope_suppresses_a_family_without_a_proven_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic narrowing rule is not proof that every returned row belongs to a tenant."""
    provider = _provider(
        {
            "inventory_scopes": [
                {
                    "name": "Tenant A",
                    "tenant_slug": "tenant-a",
                    "queries": {
                        "interface": [{"field": "name", "lookup": "contains", "value": "uplink"}]
                    },
                }
            ],
            "active_inventory_scope": "Tenant A",
        },
        token="netbox-token",
    )

    def must_not_connect(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unscoped tenant family must not reach NetBox")

    _install_transport(monkeypatch, httpx.MockTransport(must_not_connect))
    context = ProviderCallContext(provider_id="netbox:primary", operation="read_objects")

    assert await provider.list_objects("interface", context=context) == []
    pages = [page async for page in provider.iter_object_pages("interface", context=context)]
    assert pages == [[]]
    with pytest.raises(InventoryProviderError, match="suppressed"):
        await provider.get_object("interface", "7", context=context)


@pytest.mark.asyncio
async def test_saved_scope_is_anded_with_legacy_and_caller_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(
        {
            "device_query": "status=active",
            "object_queries": {"device": "role=router"},
            "inventory_scopes": [{"name": "A", "tenant_slug": "tenant-a"}],
            "active_inventory_scope": "A",
        },
        token="netbox-token",
    )
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport({"/api/dcim/devices/": {"results": [], "next": None}}, seen),
    )

    await provider.list_objects(
        "device",
        {"site": "ams01"},
        context=ProviderCallContext(provider_id="netbox:primary", operation="read_objects"),
    )

    assert seen == [
        "https://netbox.example/api/dcim/devices/?status=active&role=router&tenant=tenant-a&site=ams01&limit=100"
    ]


@pytest.mark.asyncio
async def test_netbox_ipam_objects_carry_a_provider_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(token="netbox-token")
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/ipam/vlans/": {
                    "results": [
                        {
                            "id": 5,
                            "name": "users",
                            "vid": 10,
                            "display_url": "https://netbox.example/ipam/vlans/5/",
                        }
                    ],
                    "next": None,
                }
            }
        ),
    )

    objects = await provider.list_objects(
        "vlan",
        context=ProviderCallContext(provider_id="netbox:primary", operation="list_objects"),
    )

    assert objects[0].descriptive["provider_url"] == "https://netbox.example/ipam/vlans/5/"


def test_netbox_status_map_rejects_an_unknown_maintenance_state() -> None:
    """Only states the platform models are accepted."""
    with pytest.raises(ValueError, match="status_map"):
        NetBoxProviderConfig.model_validate(
            {
                "url": "https://netbox.example",
                "token_ref": "{{ secret('vault://netbox/token') }}",
                "status_map": {"offline": "quarantined"},
            }
        )


@pytest.mark.asyncio
async def test_a_connection_test_asks_what_netbox_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/status/`` proves the token works and says which NetBox answered.

    Listing one device proves no more -- both need a valid token -- and answers nothing
    about the release or the plugins installed on it, which is exactly what an operator
    wants to see when a sync returns something they did not expect.
    """
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "netbox-version": "4.3.0",
                "netbox-full-version": "4.3.0-Docker-3.3.0",
                "plugins": {"netbox_dns": "1.1.4"},
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    result = await _provider(token="netbox-token").test_connection(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="test_connection",
            query_hash="status",
            config_version=1,
        )
    )

    assert requested == ["/api/status/"]
    assert result.ok is True
    assert result.message == "NetBox 4.3.0-Docker-3.3.0 connection succeeded"
    assert result.safe_details == {
        "netbox_version": "4.3.0",
        "netbox_full_version": "4.3.0-Docker-3.3.0",
        "netbox_plugins": {"netbox_dns": "1.1.4"},
    }


@pytest.mark.asyncio
async def test_a_refused_connection_test_reports_the_error_and_no_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe learned nothing about the instance, so it claims nothing."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Invalid token"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    result = await _provider(token="wrong-token").test_connection(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="test_connection",
            query_hash="status",
            config_version=1,
        )
    )

    assert result.ok is False
    assert result.safe_details == {}
    assert result.error is not None
    assert result.error["code"] == ProviderErrorCode.AUTH_FAILED.value


@pytest.mark.asyncio
async def test_a_netbox_that_answers_status_with_less_still_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy or an older release may reply with fewer keys; that is not a failure."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"django-version": "5.1.8"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    result = await _provider(token="netbox-token").test_connection(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="test_connection",
            query_hash="status",
            config_version=1,
        )
    )

    assert result.ok is True
    assert result.message == "NetBox connection succeeded"
    assert result.safe_details == {}


_SCHEMA_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "netbox-4.3-schema.json"
)


def _discovery_handler(
    schema: dict[str, Any] | None = None,
    *,
    custom_fields: list[dict[str, Any]] | None = None,
    schema_status: int = 200,
) -> tuple[Callable[[httpx.Request], Any], list[str]]:
    """A NetBox that answers status, schema and custom fields, recording what was asked."""
    requested: list[str] = []
    document = json.loads(_SCHEMA_FIXTURE.read_text()) if schema is None else schema

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/status/":
            return httpx.Response(200, json={"netbox-full-version": "4.3.0-Docker-3.3.0"})
        if request.url.path == "/api/schema/":
            if schema_status != 200:
                return httpx.Response(schema_status, json={"detail": "not found"})
            return httpx.Response(200, json=document)
        if request.url.path == "/api/extras/custom-fields/":
            return httpx.Response(200, json={"results": custom_fields or [], "next": None})
        return httpx.Response(404, json={"detail": "unexpected"})

    return handler, requested


async def _test_connection(provider: NetBoxInventoryProvider) -> Any:
    return await provider.test_connection(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="test_connection",
            query_hash="discovery",
            config_version=1,
        )
    )


@pytest.mark.asyncio
async def test_a_connection_test_leaves_the_schema_alone_unless_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema is several megabytes. A connection test does not fetch it by default."""
    handler, requested = _discovery_handler()
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _test_connection(_provider(token="netbox-token"))

    assert requested == ["/api/status/"]
    assert result.ok is True
    assert "netbox_families_served" not in result.safe_details


@pytest.mark.asyncio
async def test_a_discovering_connection_test_reports_what_the_instance_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asked to, it answers the question a shipped catalogue cannot: what does *this* one have."""
    handler, requested = _discovery_handler(
        custom_fields=[
            {
                "name": "hegemony_ssh_ref",
                "type": {"value": "text"},
                "object_types": ["dcim.device"],
            }
        ]
    )
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _test_connection(_provider({"schema_discovery": True}, token="netbox-token"))

    assert requested == ["/api/status/", "/api/schema/", "/api/extras/custom-fields/"]
    assert result.ok is True
    assert result.safe_details["netbox_full_version"] == "4.3.0-Docker-3.3.0"
    assert result.safe_details["netbox_families_offered"] == len(catalogue.OBJECT_TYPES)
    # Nothing undeclared, and that is the seed being complete rather than the report
    # being broken: the shipped catalogue is generated from this very schema, so a stock
    # 4.3 serves exactly what it offers. An instance with a NetBox plugin installed, or
    # on a later release, is where this becomes non-zero -- which is the number's job.
    assert result.safe_details["netbox_families_undeclared"] == 0
    assert "netbox_families_missing" not in result.safe_details
    assert result.safe_details["netbox_custom_fields"] == 1
    # A stock 4.3 serves every family the catalogue offers and no shape it cannot read.
    assert "netbox_families_missing" not in result.safe_details
    assert "netbox_schema_warnings" not in result.safe_details


@pytest.mark.asyncio
async def test_discovery_names_the_object_types_this_netbox_cannot_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer to ticking an object type and syncing nothing at all.

    An older release, or one whose plugins were trimmed, does not serve every family the
    catalogue offers. Until this, nothing said so until the first empty sync.
    """
    document = json.loads(_SCHEMA_FIXTURE.read_text())
    removed = catalogue.BY_ID["vpn_tunnel"]
    document["paths"].pop(removed.endpoint)
    handler, _requested = _discovery_handler(document)
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _test_connection(_provider({"schema_discovery": True}, token="netbox-token"))

    assert result.ok is True
    assert result.safe_details["netbox_families_missing"] == ["vpn_tunnel"]
    assert result.safe_details["netbox_families_offered"] == len(catalogue.OBJECT_TYPES) - 1


@pytest.mark.asyncio
async def test_a_schema_it_cannot_read_leaves_the_connection_test_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection already succeeded. Losing the extra detail must not undo that."""
    handler, requested = _discovery_handler(schema_status=404)
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _test_connection(_provider({"schema_discovery": True}, token="netbox-token"))

    assert "/api/schema/" in requested
    assert result.ok is True
    assert result.error is None
    assert result.safe_details == {"netbox_full_version": "4.3.0-Docker-3.3.0"}


@pytest.mark.asyncio
async def test_discovery_extends_the_registered_catalogue_rather_than_shadowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Families the plugin already knows keep the ids it registers.

    Returning the derived spelling would leave an instance offering two object types for
    the same family, and strand every row already synced under the registered one.
    """
    handler, _requested = _discovery_handler()
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _provider(token="netbox-token").discover(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="discover",
            query_hash="discover",
            config_version=1,
        )
    )

    discovered = {spec.id for spec in result.object_types}
    assert {entry.id for entry in catalogue.OBJECT_TYPES} <= discovered
    assert "dcim.interfaces" not in discovered
    # Devices and sites are families like any other now, but under the ids this plugin
    # registers: the derived spelling would strand every row already stored.
    assert {"device", "site"} <= discovered
    assert "dcim.devices" not in discovered
    assert "dcim.sites" not in discovered
    # ...but an interface still reaches its device.
    interface = next(spec for spec in result.object_types if spec.id == "interface")
    assert ("device", "device") in {
        (edge.rel, edge.target_object_type) for edge in interface.relationships
    }
    # And the instance's own families arrive under an app-qualified id.
    assert any("." in object_type for object_type in discovered)
    assert result.source_version is not None


@pytest.mark.asyncio
async def test_every_discovered_type_carries_the_mapping_to_read_it_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec without its mapping is an object type nothing can actually fetch."""
    handler, _requested = _discovery_handler()
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _provider(token="netbox-token").discover(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="discover",
            query_hash="discover",
            config_version=1,
        )
    )

    assert {spec.id for spec in result.object_types} == set(result.mappings)
    assert result.mappings["interface"].object_type == "interface"


@pytest.mark.asyncio
async def test_discovery_reads_every_custom_field_rather_than_a_preview_of_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom field past the preview cap is an attribute the catalogue would never name.

    Custom fields are read to *derive* from, not to show: each one becomes an attribute
    on every family it applies to. Stopping at the preview cap would leave the instance
    describing itself as though the fields past it were not defined -- and nothing
    downstream could tell that from an instance where they really are not. So this read
    is bounded by the page limit, which raises, rather than by a cap that truncates.
    """
    pages = [
        {
            "results": [{"name": name, "type": {"value": "text"}, "object_types": ["dcim.device"]}],
            "next": next_url,
        }
        for name, next_url in (
            ("first", "https://netbox.example/api/extras/custom-fields/?page=2"),
            ("second", "https://netbox.example/api/extras/custom-fields/?page=3"),
            ("third", None),
        )
    ]
    document = json.loads(_SCHEMA_FIXTURE.read_text())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/schema/":
            return httpx.Response(200, json=document)
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages[page - 1])

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")
    # One page's worth: under a preview cap the last two fields would never be fetched.
    provider._services.limits = InventoryLimits(
        max_provider_pages=100, max_run_targets=1000, max_preview_devices=1
    )

    result = await provider.discover(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="discover",
            query_hash="discover",
            config_version=1,
        )
    )

    device = next(spec for spec in result.object_types if spec.id == "device")
    properties = set(device.field_schema["properties"])
    assert {"custom_field_first", "custom_field_second", "custom_field_third"} <= properties


@pytest.mark.asyncio
async def test_custom_fields_that_were_read_successfully_are_not_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``warnings`` says what could not be interpreted, not what went well.

    An operator reading a discovery result should be able to treat a non-empty
    ``warnings`` as "something here needs looking at". Counting a successful read into it
    puts every well-configured instance permanently in that state.
    """
    handler, _requested = _discovery_handler(
        custom_fields=[
            {"name": "hegemony_ssh_ref", "type": {"value": "text"}, "object_types": ["dcim.device"]}
        ]
    )
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = await _provider(token="netbox-token").discover(
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="discover",
            query_hash="discover",
            config_version=1,
        )
    )

    device = next(spec for spec in result.object_types if spec.id == "device")
    # The field was read -- this is not "discovery found nothing to warn about".
    assert "custom_field_hegemony_ssh_ref" in device.field_schema["properties"]
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_discovery_raises_where_the_connection_test_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two read the same document for different reasons.

    A connection test reports on a connection that already worked, so it swallows a
    schema it cannot read. Discovery was asked for the description itself, and there is
    nothing to return in its place.
    """
    handler, _requested = _discovery_handler(schema_status=404)
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(InventoryProviderError):
        await _provider(token="netbox-token").discover(
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="discover",
                query_hash="discover",
                config_version=1,
            )
        )


def test_the_netbox_provider_declares_the_capability_it_implements() -> None:
    assert Capability.DISCOVER in _provider().capabilities


@pytest.mark.asyncio
async def test_netbox_reads_a_family_only_the_instance_knows_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NetBox plugin's model is readable although no catalogue has ever named it.

    This is the payoff of discovery and the one thing a shipped catalogue can never do:
    ``netbox_dns.records`` exists on one operator's NetBox and nowhere else. The provider
    is built fresh for the call and has never heard of it, so everything it needs --
    where to fetch it and how to read a record -- arrives on the call context, exactly as
    the platform stored it when the instance described itself.
    """
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(
            str(request.url.path) + ("?" + request.url.query.decode() if request.url.query else "")
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 12,
                        "name": "www",
                        "zone": {"name": "example.com"},
                        "type": "A",
                        "value": "10.0.0.8",
                    }
                ],
                "next": None,
            },
        )

    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    mapping = ObjectFieldMapping(
        object_type="netbox_dns.records",
        identity="id",
        name_field="name",
        field_map={"name": "name", "zone": "zone.name", "type": "type", "value": "value"},
        source={"endpoint": "/api/plugins/netbox-dns/records/"},
    )
    objects = await provider.list_objects(
        "netbox_dns.records",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="auto_sync_objects:netbox_dns.records",
            object_mapping=mapping,
        ),
    )

    assert paths == ["/api/plugins/netbox-dns/records/?limit=100"]
    (record,) = objects
    assert (record.object_type, record.external_id, record.name) == (
        "netbox_dns.records",
        "12",
        "www",
    )
    assert record.attributes == {
        "name": "www",
        "zone": "example.com",
        "type": "A",
        "value": "10.0.0.8",
    }


@pytest.mark.asyncio
async def test_discovered_only_family_uses_its_instances_query_schema_for_tenant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        return httpx.Response(200, json={"results": [], "next": None})

    provider = _provider(
        {
            "inventory_scopes": [{"name": "Tenant A", "tenant_slug": "tenant-a"}],
            "active_inventory_scope": "Tenant A",
        },
        token="netbox-token",
    )
    _install_transport(monkeypatch, httpx.MockTransport(handler))
    mapping = ObjectFieldMapping(
        object_type="custom.widgets",
        identity="id",
        name_field="name",
        field_map={"name": "name", "tenant": "tenant.slug"},
        source={"endpoint": "/api/plugins/custom/widgets/"},
    )

    await provider.list_objects(
        "custom.widgets",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="auto_sync_objects:custom.widgets",
            object_mapping=mapping,
            object_query_schema={
                "type": "object",
                "properties": {"tenant": {"type": "string"}},
            },
        ),
    )

    assert paths == ["https://netbox.example/api/plugins/custom/widgets/?tenant=tenant-a&limit=100"]


@pytest.mark.asyncio
async def test_a_catalogue_family_gains_the_custom_fields_only_this_instance_defines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance's half of a family the plugin already knows.

    ``interface`` is in the shipped catalogue, so the endpoint, the key and the paths all
    come from there. But a custom field is defined on one NetBox and not the next, and
    discovery advertises it: the platform merges it into the object type it publishes, so
    a browse page offers the column. If the read ignored it, that column would be empty
    for every row on every instance -- an attribute promised and never delivered.
    """
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url.path))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 7,
                        "name": "et-0/0/0",
                        "custom_fields": {"optic_serial": "OPT-991"},
                    }
                ],
                "next": None,
            },
        )

    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(handler))

    shipped = BY_ID["interface"].mapping
    discovered = ObjectFieldMapping(
        object_type="interface",
        identity="id",
        name_field="name",
        field_map={
            **shipped.field_map,
            "custom_field_optic_serial": "custom_fields.optic_serial",
            # A path the instance disagrees with the plugin about. The overlay's
            # judgement is the plugin's to keep; discovery adds, it does not overwrite.
            "name": "display",
        },
        source={"endpoint": "/api/plugins/wrong/"},
    )
    objects = await provider.list_objects(
        "interface",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="auto_sync_objects:interface",
            object_mapping=discovered,
        ),
    )

    # Read from the catalogue's endpoint, not the discovered mapping's.
    assert paths == [BY_ID["interface"].endpoint]
    (record,) = objects
    assert record.attributes["custom_field_optic_serial"] == "OPT-991"
    # The plugin's path for a field both describe still wins.
    assert record.attributes["name"] == "et-0/0/0"


@pytest.mark.asyncio
async def test_netbox_refuses_a_family_nothing_has_described(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a mapping there is nowhere to read from, and the provider says so.

    The counterpart of the previous test: being handed nothing must not become a guess
    at an endpoint. A family the catalogue does not carry and the platform cannot
    describe falls through to the SDK default, which reports that it is not supported.
    """
    provider = _provider(token="netbox-token")
    _install_transport(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(200)))

    with pytest.raises(InventoryProviderError) as raised:
        await provider.list_objects(
            "netbox_dns.records",
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="auto_sync_objects:netbox_dns.records",
            ),
        )
    assert raised.value.envelope.code == ProviderErrorCode.MALFORMED_QUERY


# ── The retired device/site contract ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", [ResourceType.DEVICE, ResourceType.SITE])
async def test_the_device_contract_refuses_and_says_what_to_do(
    resource: ResourceType,
) -> None:
    """Loud, not quiet, and it names the fix.

    This provider no longer answers for devices and sites through the platform's
    device/site contract -- they are object families and the platform writes those
    tables from the stored rows. The alternative to failing here was to decline to serve
    them at all, which would let a sync succeed while quietly refreshing nothing: an
    operator would find out weeks later, from a run that targeted a device whose address
    had changed. A sync that fails once and says "run Discover" is the kinder failure.
    """
    provider = _provider(token="netbox-token")
    context = ProviderCallContext(provider_id="netbox:primary", operation="auto_sync")

    with pytest.raises(InventoryProviderError) as raised:
        await provider.list(resource, context=context)

    envelope = raised.value.envelope
    assert envelope.retryable is False, "pressing Sync again will never help"
    assert "Discover" in envelope.message


@pytest.mark.asyncio
async def test_query_devices_and_list_sites_refuse_the_same_way() -> None:
    """The two the platform calls during a sync, and the reason they cannot just go.

    Both are ``@abstractmethod`` on the SDK's provider base, so a provider that does not
    implement them cannot be instantiated at all. They stay, and they refuse.
    """
    provider = _provider(token="netbox-token")
    context = ProviderCallContext(provider_id="netbox:primary", operation="auto_sync")

    # Built when called, not when the tuple is: a tuple of coroutines would leave the
    # second one un-awaited if the first assertion failed, and the resulting warning
    # buries the failure that actually matters.
    for call in (
        lambda: provider.query_devices("", context=context),
        lambda: provider.list_sites(context=context),
    ):
        with pytest.raises(InventoryProviderError) as raised:
            await call()
        assert "Discover" in raised.value.envelope.message


@pytest.mark.asyncio
async def test_a_family_that_is_not_a_device_still_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring one contract must not take the rest of the catalogue with it."""
    _install_transport(
        monkeypatch,
        _json_transport(
            {"/api/dcim/interfaces/": {"results": [{"id": 7, "name": "et-0/0/0"}], "next": None}}
        ),
    )

    objects = await _provider(token="netbox-token").list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="read_objects"),
    )

    assert [obj.external_id for obj in objects] == ["7"]


def _bytes_response(payload: bytes, *, declare_length: bool) -> httpx.Response:
    """A response that streams ``payload``, with or without a Content-Length header.

    A chunked response declares no length, which is exactly the case a header check
    cannot decide and the running total has to.
    """
    if declare_length:
        return httpx.Response(200, content=payload)
    return httpx.Response(
        200,
        stream=_ChunkedStream(payload),
        headers={"content-type": "application/json"},
    )


class _ChunkedStream(httpx.AsyncByteStream):
    """Hands the body over in small pieces and never declares its length."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> Any:
        for start in range(0, len(self._payload), 1024):
            yield self._payload[start : start + 1024]


@pytest.mark.asyncio
@pytest.mark.parametrize("declare_length", [True, False])
async def test_a_response_past_the_cap_is_refused_unparsed(
    monkeypatch: pytest.MonkeyPatch, declare_length: bool
) -> None:
    """Valid JSON is still refused once it is too big, declared length or not.

    The payload parses perfectly. Size is the whole objection, so a cap that only
    rejected malformed bodies would not be this cap.
    """
    payload = b'{"results": [' + b'{"id": 1, "name": "x"},' * 20_000 + b'{"id": 2}], "next": null}'
    assert len(payload) > 128 * 1024

    async def handler(request: httpx.Request) -> httpx.Response:
        return _bytes_response(payload, declare_length=declare_length)

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider({"max_response_bytes": 64 * 1024}, token="netbox-token")

    with pytest.raises(InventoryProviderError) as raised:
        await provider.list_objects(
            "interface",
            context=ProviderCallContext(provider_id="netbox:primary", operation="cap-test"),
        )

    envelope = raised.value.envelope
    assert envelope.code == ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    # Not retryable: the same read returns the same oversized body.
    assert envelope.retryable is False
    assert envelope.safe_details["max_response_bytes"] == 64 * 1024


@pytest.mark.asyncio
async def test_a_response_under_the_cap_is_read_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is a ceiling, not a toll: an ordinary body is unaffected."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": 7, "name": "eth0"}], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider({"max_response_bytes": 64 * 1024}, token="netbox-token")

    objects = await provider.list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="cap-test"),
    )

    assert [obj.external_id for obj in objects] == ["7"]


@pytest.mark.asyncio
async def test_a_lying_content_length_does_not_get_past_the_running_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared length under the cap decides nothing; the bytes actually read do.

    The header is the sender's claim about itself, so treating it as the check would
    make the cap advisory for anyone who cared to evade it.
    """
    payload = b'{"results": [' + b'{"id": 1, "name": "x"},' * 20_000 + b'{"id": 2}], "next": null}'

    async def handler(request: httpx.Request) -> httpx.Response:
        response = _bytes_response(payload, declare_length=False)
        response.headers["content-length"] = "128"
        return response

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider({"max_response_bytes": 64 * 1024}, token="netbox-token")

    with pytest.raises(InventoryProviderError) as raised:
        await provider.list_objects(
            "interface",
            context=ProviderCallContext(provider_id="netbox:primary", operation="cap-test"),
        )

    assert raised.value.envelope.safe_details["max_response_bytes"] == 64 * 1024


@pytest.mark.asyncio
async def test_a_read_with_no_marker_asks_for_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first sync has nothing to narrow by, and must not narrow by anything."""
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")

    await provider.list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="auto_sync"),
    )

    assert "last_updated__gte" not in captured["url"]


@pytest.mark.asyncio
async def test_a_read_with_a_marker_asks_only_for_what_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker becomes a NetBox filter, inclusive of its own boundary.

    ``__gte`` rather than ``__gt``: two objects saved in the same instant carry the same
    timestamp, and excluding the boundary would drop whichever of them the previous read
    did not reach. Re-reading one costs an upsert that changes nothing.
    """
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")

    await provider.list_objects(
        "interface",
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="auto_sync",
            since="2026-09-15T12:00:00.000000Z",
        ),
    )

    assert "last_updated__gte=2026-09-15T12%3A00%3A00.000000Z" in captured["url"]


@pytest.mark.asyncio
async def test_a_marker_narrows_rather_than_replaces_the_configured_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider scoped to part of an estate stays scoped to it when reading a delta.

    Losing the configured filter here would widen a deliberately narrow provider to the
    whole table on every delta pass, which is the opposite of what delta is for.
    """
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": [], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider({"object_queries": {"interface": "mgmt_only=false"}}, token="netbox-token")

    await provider.list_objects(
        "interface",
        context=ProviderCallContext(
            provider_id="netbox:primary", operation="auto_sync", since="2026-09-15T12:00:00Z"
        ),
    )

    assert "mgmt_only=false" in captured["url"]
    assert "last_updated__gte" in captured["url"]


@pytest.mark.asyncio
async def test_every_object_carries_the_marker_the_next_delta_read_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filtering by ``since`` and reporting no marker is the one unsafe combination.

    The platform would go on asking for changes since a moment that never advanced, and
    believe itself up to date while the source moved on.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 7, "name": "eth0", "last_updated": "2026-09-15T12:00:00.123456Z"},
                    {"id": 8, "name": "eth1", "last_updated": "2026-09-15T13:00:00.000000Z"},
                ],
                "next": None,
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")

    objects = await provider.list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="auto_sync"),
    )

    assert [obj.source_version["marker"] for obj in objects] == [
        "2026-09-15T12:00:00.123456Z",
        "2026-09-15T13:00:00.000000Z",
    ]


@pytest.mark.asyncio
async def test_a_record_without_a_timestamp_simply_carries_no_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin model may not stamp last_updated; that is a missing marker, not a crash.

    The platform keeps the highest marker it saw, so a record contributing none just
    does not raise the watermark -- and will be read again next time, which is right.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": 7, "name": "eth0"}], "next": None})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    provider = _provider(token="netbox-token")

    objects = await provider.list_objects(
        "interface",
        context=ProviderCallContext(provider_id="netbox:primary", operation="auto_sync"),
    )

    assert "marker" not in objects[0].source_version


def test_netbox_declares_it_can_answer_what_changed() -> None:
    """The capability is the promise core checks before asking for a delta at all."""
    assert Capability.DELTA in _provider().capabilities
