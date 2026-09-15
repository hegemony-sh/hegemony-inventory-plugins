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
from hegemony_inventory_netbox.provider import NetBoxInventoryProvider
from hegemony_inventory_sdk import (
    InventoryProviderError,
    MaintenanceState,
    ProviderCallContext,
    ProviderErrorCode,
    ResourceType,
    SiteRef,
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
    await provider.query_devices(
        "",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="query_devices",
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
    await provider.query_devices(
        "",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="query_devices",
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
        await provider.query_devices(
            "bad=query",
            limit=10,
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="query_devices",
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
    await provider.query_devices(
        "",
        limit=10,
        context=ProviderCallContext(
            provider_id="netbox:primary",
            operation="query_devices",
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
        await provider.query_devices(
            "",
            limit=10,
            context=ProviderCallContext(
                provider_id="netbox:primary",
                operation="query_devices",
                query_hash="pagination-origin-test",
                config_version=1,
            ),
        )

    assert exc_info.value.envelope.code == ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    assert captured == [("netbox.example", "Bearer netbox-token")]


def test_netbox_mapping_does_not_stringify_none_values() -> None:
    provider = _provider()

    descriptor = provider._map_device(
        {
            "id": 123,
            "name": "router-no-platform",
            "primary_ip4": {"address": "192.0.2.10/32"},
            "platform": {"slug": None},
            "device_type": {"model": None},
        }
    )

    assert descriptor.platform is None
    assert descriptor.model is None


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
        "platform": "platform.slug",
        "access_config.ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
    }


def test_netbox_mapping_uses_generic_field_map_and_default_access_config() -> None:
    provider = _provider(
        {
            "field_map": {
                "external_id": "id",
                "name": "name",
                "display_name": "display",
                "hostname": "hostname",
                "mgmt_host": "primary_ip.address",
                "platform": "platform.slug",
                "model": "device_type.model",
                "site.external_id": "site.slug",
                "site.name": "site.name",
                "role": "device_role.slug",
                "tags": "tags",
                "access_config.ssh.username_ref": "custom_fields.net_ssh_user",
                "access_config.ssh.password_ref": "custom_fields.net_ssh_password",
            },
            "default_access_config": {
                "ssh": {"private_key_ref": "{{ secret('vault://shared/netbox/key') }}"},
                "enable": {"password_ref": "{{ secret('vault://shared/netbox/enable') }}"},
            },
        }
    )

    descriptor = provider._map_device(
        {
            "id": 123,
            "name": "router-1",
            "display": "Router 1",
            "hostname": "router-1.example",
            "primary_ip": {"address": "192.0.2.10/32"},
            "platform": {"slug": "junos"},
            "device_type": {"model": "MX204"},
            "site": {"slug": "prg1", "name": "Prague 1"},
            "device_role": {"slug": "edge"},
            "tags": [{"slug": "wan"}, {"name": "core"}],
            "custom_fields": {
                "net_ssh_user": "{{ secret('vault://devices/router-1/user') }}",
                "net_ssh_password": "{{ secret('vault://devices/router-1/password') }}",
            },
        }
    )

    assert descriptor.external_id == "123"
    assert descriptor.display_name == "Router 1"
    assert descriptor.hostname == "router-1.example"
    assert descriptor.mgmt_host == "192.0.2.10"
    assert descriptor.platform == "junos"
    assert descriptor.model == "MX204"
    assert descriptor.role == "edge"
    assert descriptor.site is not None
    assert descriptor.site.external_id == "prg1"
    assert descriptor.site.name == "Prague 1"
    assert descriptor.native_tags == ("wan", "core")
    assert descriptor.access_config == {
        "ssh": {
            "username_ref": "{{ secret('vault://devices/router-1/user') }}",
            "password_ref": "{{ secret('vault://devices/router-1/password') }}",
            "private_key_ref": "{{ secret('vault://shared/netbox/key') }}",
        },
        "enable": {
            "password_ref": "{{ secret('vault://shared/netbox/enable') }}",
        },
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
    assert objects[0].attributes == {
        "prefix": "10.0.0.0/24",
        "status": "active",
        "vlan": 10,
        "role": "lan",
        "site": "ams01",
        "scope": None,
        "scope_type": None,
        "vrf": None,
        "tenant": None,
        "is_pool": False,
        "description": "core",
    }
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
    assert vlan.attributes == {
        "vid": 100,
        "name": "prod-web",
        "status": "active",
        "site": "ams01",
        "group": "campus",
        "role": "server",
        "tenant": None,
        "description": "web tier",
    }


def test_netbox_merges_default_jump_host_onto_every_device() -> None:
    """A fleet-wide bastion in default_access_config reaches every device.

    Unlike the git provider — whose per-device YAML can name its own hop —
    NetBox exposes only the provider-level default: the mappable
    access_config paths are fixed by DEFAULT_FIELD_MAP, which carries the
    ssh/enable credential refs and no jump_host entry. This pins that
    boundary along with the merge itself.
    """
    provider = _provider(
        {
            "field_map": {
                "external_id": "id",
                "name": "name",
                "mgmt_host": "primary_ip.address",
                "access_config.ssh.username_ref": "custom_fields.net_ssh_user",
            },
            "default_access_config": {
                "ssh": {"private_key_ref": "{{ secret('vault://shared/netbox/key') }}"},
                "jump_host": {
                    "host": "dind",
                    "port": 2222,
                    "username_ref": "{{ secret('vault://lab/bastion/username') }}",
                    "password_ref": "{{ secret('vault://lab/bastion/password') }}",
                },
            },
        }
    )

    descriptor = provider._map_device(
        {
            "id": 1,
            "name": "r1",
            "primary_ip": {"address": "192.0.2.10/32"},
            "custom_fields": {"net_ssh_user": "{{ secret('vault://devices/r1/user') }}"},
        }
    )

    assert descriptor.access_config["jump_host"] == {
        "host": "dind",
        "port": 2222,
        "username_ref": "{{ secret('vault://lab/bastion/username') }}",
        "password_ref": "{{ secret('vault://lab/bastion/password') }}",
    }
    # The scope is additive: mapped and defaulted ssh refs still merge.
    assert descriptor.access_config["ssh"] == {
        "username_ref": "{{ secret('vault://devices/r1/user') }}",
        "private_key_ref": "{{ secret('vault://shared/netbox/key') }}",
    }


def test_netbox_without_default_jump_host_emits_no_jump_host_scope() -> None:
    # An empty scope must not appear at all: a jump_host block with no host
    # would make the platform attempt a bastion hop to nowhere.
    provider = _provider(
        {
            "field_map": {"external_id": "id", "name": "name", "mgmt_host": "primary_ip.address"},
            "default_access_config": {"ssh": {"username_ref": "{{ secret('vault://a/b') }}"}},
        }
    )
    descriptor = provider._map_device(
        {"id": 1, "name": "r1", "primary_ip": {"address": "192.0.2.10/32"}, "custom_fields": {}}
    )
    assert "jump_host" not in descriptor.access_config


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
async def test_netbox_site_external_id_matches_between_site_list_and_device_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site keys identically whether it arrives via /sites/ or nested in a device.

    The two paths used to disagree — ``_map_site`` emitted the numeric NetBox id
    while the device field map emitted ``site.slug`` — so the platform, which keys
    sites on ``(provider_id, external_id)``, materialized every NetBox site twice
    and attached devices to the copy carrying no parent link.
    """
    provider = _provider(token="netbox-token")
    site_payload = {"id": 7, "slug": "prg1", "name": "Prague 1", "physical_address": "Praha"}
    _install_transport(
        monkeypatch,
        _json_transport({"/api/dcim/sites/": {"results": [site_payload], "next": None}}),
    )

    listed = await provider.list_sites(
        context=ProviderCallContext(provider_id="netbox:primary", operation="list_sites")
    )
    nested = provider._map_device(
        {"id": 1, "name": "router-1", "site": {"id": 7, "slug": "prg1", "name": "Prague 1"}}
    )

    assert [site.external_id for site in listed] == ["prg1"]
    assert nested.site is not None
    assert nested.site.external_id == listed[0].external_id


def test_netbox_site_parent_is_keyed_like_its_children() -> None:
    """A parent reference uses the same key as the sites it parents.

    Emitting a numeric parent id against slug-keyed children left every parent
    link dangling, which is what disconnected the NetBox site hierarchy.
    """
    provider = _provider()

    site = provider._map_site(
        {
            "id": 7,
            "slug": "prg1",
            "name": "Prague 1",
            "parent": {"id": 3, "slug": "cz", "name": "Czechia"},
        }
    )

    assert site.external_id == "prg1"
    assert site.parent_external_id == "cz"


def test_netbox_site_key_follows_the_configured_field_map() -> None:
    """Remapping ``site.external_id`` moves both mapping paths together."""
    provider = _provider({"field_map": {"site.external_id": "site.id"}})

    site = provider._map_site({"id": 7, "slug": "prg1", "name": "Prague 1"})
    device = provider._map_device({"id": 1, "name": "r1", "site": {"id": 7, "slug": "prg1"}})

    assert site.external_id == "7"
    assert device.site is not None
    assert device.site.external_id == "7"


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

    assert seen == ["https://netbox.example/api/ipam/vlans/?site=prg1&status=active"]


@pytest.mark.asyncio
async def test_netbox_list_pushes_the_query_for_devices_and_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list()`` honours its ``query`` argument for both resource types."""
    provider = _provider(token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/devices/": {"results": [], "next": None},
                "/api/dcim/sites/": {"results": [], "next": None},
            },
            seen,
        ),
    )
    context = ProviderCallContext(provider_id="netbox:primary", operation="list")

    await provider.list(
        netbox_provider_module.ResourceType.DEVICE, {"role": "edge"}, context=context
    )
    await provider.list(netbox_provider_module.ResourceType.SITE, {"group": "eu"}, context=context)

    assert seen == [
        "https://netbox.example/api/dcim/devices/?role=edge",
        "https://netbox.example/api/dcim/sites/?group=eu",
    ]


def test_netbox_tags_become_filterable_device_tags() -> None:
    """NetBox tag slugs land in the key/value tag map the platform filters on.

    Native tags alone are not reachable from device search or a run form's
    ``tag.*`` filter, both of which read the ``tags`` map.
    """
    provider = _provider()

    descriptor = provider._map_device(
        {
            "id": 1,
            "name": "router-1",
            "role": {"slug": "edge"},
            "tags": [{"slug": "core"}, {"name": "wan"}],
        }
    )

    assert descriptor.tags == {"role": "edge", "tag:core": "true", "tag:wan": "true"}
    assert descriptor.native_tags == ("core", "wan")


def test_netbox_tag_key_prefix_is_configurable_and_avoids_role_collisions() -> None:
    """The prefix is what keeps a NetBox tag named ``role`` from shadowing the role."""
    prefixed = _provider()._map_device(
        {"id": 1, "name": "r1", "role": {"slug": "edge"}, "tags": [{"slug": "role"}]}
    )
    bare = _provider({"tag_key_prefix": ""})._map_device(
        {"id": 1, "name": "r1", "tags": [{"slug": "core"}]}
    )

    assert prefixed.tags == {"role": "edge", "tag:role": "true"}
    assert bare.tags == {"core": "true"}


def test_netbox_maps_manufacturer_to_vendor_and_extras_to_descriptive() -> None:
    """Vendor is a first-class device column the UI filters on; it was always None.

    Serial, asset tag, rack, position, tenant and location have no device column,
    so they ride along in ``descriptive``, which is GIN-indexed and API-visible.
    """
    provider = _provider()

    descriptor = provider._map_device(
        {
            "id": 1,
            "name": "router-1",
            "device_type": {"model": "MX204", "manufacturer": {"name": "Juniper"}},
            "serial": "ABC123",
            "asset_tag": "ASSET-9",
            "rack": {"name": "R12"},
            "position": 21,
            "tenant": {"slug": "acme"},
            "location": {"slug": "hall-a"},
            "primary_ip6": {"address": "2001:db8::10/128"},
        }
    )

    assert descriptor.vendor == "Juniper"
    assert descriptor.model == "MX204"
    assert descriptor.descriptive["serial"] == "ABC123"
    assert descriptor.descriptive["asset_tag"] == "ASSET-9"
    assert descriptor.descriptive["rack"] == "R12"
    assert descriptor.descriptive["position"] == 21
    assert descriptor.descriptive["tenant"] == "acme"
    assert descriptor.descriptive["location"] == "hall-a"
    assert descriptor.descriptive["mgmt_host_v6"] == "2001:db8::10"


def test_netbox_omits_descriptive_extras_a_device_does_not_carry() -> None:
    """A device with none of the optional fields gains no empty keys."""
    descriptor = _provider()._map_device({"id": 1, "name": "router-1"})

    for absent in ("serial", "asset_tag", "rack", "position", "tenant", "mgmt_host_v6"):
        assert absent not in descriptor.descriptive
    assert descriptor.vendor is None


def test_netbox_device_provider_url_prefers_display_url() -> None:
    """The deep link comes from NetBox's own URL fields, not a constructed path."""
    provider = _provider()

    from_display = provider._map_device(
        {
            "id": 1,
            "name": "r1",
            "display_url": "https://netbox.example/dcim/devices/1/",
            "url": "https://netbox.example/api/dcim/devices/1/",
        }
    )
    from_api_url = provider._map_device(
        {"id": 2, "name": "r2", "url": "https://netbox.example/api/dcim/devices/2/"}
    )

    assert from_display.descriptive["provider_url"] == "https://netbox.example/dcim/devices/1/"
    assert from_api_url.descriptive["provider_url"] == "https://netbox.example/dcim/devices/2/"


def test_netbox_provider_url_rejects_an_off_origin_link() -> None:
    """A NetBox echoing another host must not become an off-site redirect."""
    descriptor = _provider()._map_device(
        {"id": 1, "name": "r1", "display_url": "https://evil.example/dcim/devices/1/"}
    )

    assert "provider_url" not in descriptor.descriptive


def test_netbox_site_carries_a_provider_url() -> None:
    site = _provider()._map_site(
        {
            "id": 7,
            "slug": "prg1",
            "name": "Prague 1",
            "display_url": "https://netbox.example/dcim/sites/7/",
        }
    )

    assert site.descriptive["provider_url"] == "https://netbox.example/dcim/sites/7/"
    assert site.descriptive["slug"] == "prg1"


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
    context = ProviderCallContext(provider_id="netbox:primary", operation="query_devices")

    await provider.query_devices("", context=context)
    await provider.query_devices("site=prg1", context=context)

    assert seen == [
        "https://netbox.example/api/dcim/devices/?status=active&role=wan-edge",
        "https://netbox.example/api/dcim/devices/?status=active&role=wan-edge&site=prg1",
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"value": "active", "label": "Active"}, None),
        ({"value": "offline"}, "disabled"),
        ({"value": "decommissioning"}, "disabled"),
        ({"value": "planned"}, "maintenance"),
        ({"value": "staged"}, "maintenance"),
        ({"value": "failed"}, "maintenance"),
        ({"value": "inventory"}, "maintenance"),
        ("offline", "disabled"),  # older payloads send a bare slug
        (None, None),
        ({"value": "something-custom"}, None),
    ],
)
def test_netbox_device_status_maps_to_maintenance_state(status: Any, expected: str | None) -> None:
    """NetBox lifecycle status becomes the platform's maintenance state.

    An unmapped status yields None so the device's existing state is left alone
    rather than being forced back to active.
    """
    descriptor = _provider()._map_device({"id": 1, "name": "r1", "status": status})

    assert (
        descriptor.maintenance_state.value if descriptor.maintenance_state else None
    ) == expected


def test_netbox_status_map_is_configurable() -> None:
    """A deployment can decide what its own statuses mean."""
    provider = _provider({"status_map": {"active": "maintenance", "offline": "active"}})

    frozen = provider._map_device({"id": 1, "name": "r1", "status": {"value": "active"}})
    thawed = provider._map_device({"id": 2, "name": "r2", "status": {"value": "offline"}})
    # 'planned' is no longer in the map, so it stops being mapped at all.
    unmapped = provider._map_device({"id": 3, "name": "r3", "status": {"value": "planned"}})

    assert frozen.maintenance_state is MaintenanceState.MAINTENANCE
    assert thawed.maintenance_state is MaintenanceState.ACTIVE
    assert unmapped.maintenance_state is None


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
async def test_netbox_get_site_resolves_a_slug_through_the_list_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default site key is a slug, which NetBox's detail route cannot take.

    ``list_sites`` emits ``field_map['site.external_id']`` — ``site.slug`` by
    default — so a detail read of the value it just handed out would hit
    ``/api/dcim/sites/prg1/`` and 404. It has to go through a filter first.
    """
    provider = _provider(token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/sites/": {
                    "results": [{"id": 7, "slug": "prg1", "name": "Prague 1"}],
                    "next": None,
                }
            },
            seen,
        ),
    )

    site = await provider.get(
        ResourceType.SITE,
        "prg1",
        context=ProviderCallContext(provider_id="netbox:primary", operation="get"),
    )

    assert seen == ["https://netbox.example/api/dcim/sites/?slug=prg1"]
    assert isinstance(site, SiteRef)
    assert site.external_id == "prg1"
    assert site.name == "Prague 1"


@pytest.mark.asyncio
async def test_netbox_get_site_uses_the_detail_route_when_keyed_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remapping the key to the numeric id keeps the cheaper single-object read."""
    provider = _provider({"field_map": {"site.external_id": "site.id"}}, token="netbox-token")
    seen: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {"/api/dcim/sites/7/": {"id": 7, "slug": "prg1", "name": "Prague 1"}}, seen
        ),
    )

    site = await provider.get(
        ResourceType.SITE,
        "7",
        context=ProviderCallContext(provider_id="netbox:primary", operation="get"),
    )

    assert seen == ["https://netbox.example/api/dcim/sites/7/"]
    assert isinstance(site, SiteRef)
    assert site.external_id == "7"


@pytest.mark.asyncio
async def test_netbox_get_site_fails_closed_when_the_filter_matches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slug NetBox does not know is NOT_FOUND, not a mis-keyed match."""
    provider = _provider(token="netbox-token")
    _install_transport(
        monkeypatch,
        _json_transport({"/api/dcim/sites/": {"results": [], "next": None}}),
    )

    with pytest.raises(InventoryProviderError) as excinfo:
        await provider.get(
            ResourceType.SITE,
            "nope",
            context=ProviderCallContext(provider_id="netbox:primary", operation="get"),
        )

    assert excinfo.value.envelope.code == ProviderErrorCode.NOT_FOUND


def test_netbox_a_native_tag_never_overwrites_the_mapped_role() -> None:
    """With an empty prefix, a NetBox tag named ``role`` must lose to the role.

    ``tag_key_prefix: ""`` is documented as supported, and the collision it
    reopens would leave ``tags[role]`` reading ``"true"`` instead of the role —
    silently dropping the device out of every role filter.
    """
    device = _provider({"tag_key_prefix": ""})._map_device(
        {"id": 1, "name": "r1", "role": {"slug": "edge"}, "tags": [{"slug": "role"}]}
    )

    assert device.tags == {"role": "edge"}


# --- the site tree -------------------------------------------------------------------
# NetBox keeps two independent nested hierarchies above a site -- Region and Site
# Group -- and a platform Site has one parent, so the provider has to choose. These
# cover the choice, what happens to the axis that loses, and the mechanics that make
# synthesized nodes safe to materialize alongside real sites.

_SITES_WITH_HIERARCHY = {
    "results": [
        {
            "id": 1,
            "slug": "mt1",
            "name": "Minas Tirith",
            "region": {"id": 20, "slug": "gondor", "name": "Gondor"},
            "group": {"id": 30, "slug": "datacenter", "name": "Datacenter"},
        },
        {
            "id": 2,
            "slug": "hob1",
            "name": "Hobbiton",
            "region": {"id": 21, "slug": "eriador", "name": "Eriador"},
            "group": {"id": 31, "slug": "branch", "name": "Branch"},
        },
    ],
    "next": None,
}

_REGIONS = {
    "results": [
        {"id": 10, "slug": "middle-earth", "name": "Middle-earth", "parent": None},
        {
            "id": 20,
            "slug": "gondor",
            "name": "Gondor",
            "parent": {"id": 10, "slug": "middle-earth", "name": "Middle-earth"},
        },
        {
            "id": 21,
            "slug": "eriador",
            "name": "Eriador",
            "parent": {"id": 10, "slug": "middle-earth", "name": "Middle-earth"},
        },
        # Referenced by no synced site, so it must not be emitted.
        {
            "id": 22,
            "slug": "mordor",
            "name": "Mordor",
            "parent": {"id": 10, "slug": "middle-earth", "name": "Middle-earth"},
        },
    ],
    "next": None,
}

_SITE_GROUPS = {
    "results": [
        {"id": 30, "slug": "datacenter", "name": "Datacenter", "parent": None},
        {"id": 31, "slug": "branch", "name": "Branch", "parent": None},
    ],
    "next": None,
}


def _hierarchy_transport(
    monkeypatch: pytest.MonkeyPatch, sites: Any = _SITES_WITH_HIERARCHY
) -> None:
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/sites/": sites,
                "/api/dcim/regions/": _REGIONS,
                "/api/dcim/site-groups/": _SITE_GROUPS,
            }
        ),
    )


async def _listed(provider: NetBoxInventoryProvider) -> list[Any]:
    return await provider.list_sites(
        context=ProviderCallContext(provider_id="netbox:primary", operation="list_sites")
    )


@pytest.mark.asyncio
async def test_regions_become_the_site_tree_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NetBox site has no parent of its own; its region is the hierarchy.

    Without this the platform received a flat list -- ``_map_site`` looked for a
    ``parent`` key that NetBox never sends -- and the site tree, which the API
    exposes and "include child sites" targeting depends on, had nothing in it.
    """
    provider = _provider(token="netbox-token")
    _hierarchy_transport(monkeypatch)

    listed = await _listed(provider)
    by_id = {site.external_id: site for site in listed}

    # The regions arrive as sites, nested by their own parent...
    assert by_id["region:middle-earth"].parent_external_id is None
    assert by_id["region:gondor"].parent_external_id == "region:middle-earth"
    assert by_id["region:eriador"].parent_external_id == "region:middle-earth"
    # ...and the real sites hang from them.
    assert by_id["mt1"].parent_external_id == "region:gondor"
    assert by_id["hob1"].parent_external_id == "region:eriador"


@pytest.mark.asyncio
async def test_only_the_ancestors_of_synced_sites_are_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped sync must not drag in the whole of NetBox's geography.

    ``device_query`` exists so an operator can sync 300 devices out of 50,000;
    emitting every region regardless would undo that for sites, and NetBox's
    region tree can describe an entire estate the platform never syncs.
    """
    provider = _provider(token="netbox-token")
    _hierarchy_transport(monkeypatch)

    listed = await _listed(provider)

    ids = {site.external_id for site in listed}
    assert "region:mordor" not in ids, "an unreferenced region was materialized"
    # Its parent is still there, because two synced sites do descend from it.
    assert "region:middle-earth" in ids


@pytest.mark.asyncio
async def test_both_hierarchy_axes_are_tagged_whichever_one_is_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing one axis must not lose the other.

    Only one of region and site group can be the parent, but an operator still
    wants to select "every branch site" or "everything in Gondor". Both are
    surfaced as tags so the axis that lost stays usable for filtering.
    """
    provider = _provider(token="netbox-token")
    _hierarchy_transport(monkeypatch)

    listed = await _listed(provider)
    mt1 = next(site for site in listed if site.external_id == "mt1")

    assert mt1.tags == {"region": "gondor", "group": "datacenter"}


@pytest.mark.asyncio
async def test_site_group_can_be_the_tree_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other axis, for estates modelled by function rather than geography."""
    provider = _provider({"site_tree_source": "site_group"}, token="netbox-token")
    _hierarchy_transport(monkeypatch)

    listed = await _listed(provider)
    by_id = {site.external_id: site for site in listed}

    assert by_id["mt1"].parent_external_id == "site-group:datacenter"
    assert by_id["hob1"].parent_external_id == "site-group:branch"
    assert "region:gondor" not in by_id, "the unchosen axis must not also be materialized"
    # ...and the region is still a tag.
    assert by_id["mt1"].tags["region"] == "gondor"


@pytest.mark.asyncio
async def test_site_tree_source_none_keeps_the_flat_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous behaviour stays reachable, and costs no extra request."""
    provider = _provider({"site_tree_source": "none"}, token="netbox-token")
    calls: list[str] = []
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/sites/": _SITES_WITH_HIERARCHY,
                "/api/dcim/regions/": _REGIONS,
            },
            recorder=calls,
        ),
    )

    listed = await _listed(provider)

    assert [site.external_id for site in listed] == ["mt1", "hob1"]
    assert all(site.parent_external_id is None for site in listed)
    assert not any("/regions/" in call for call in calls), (
        "the hierarchy endpoint was fetched even though no tree was asked for"
    )


@pytest.mark.asyncio
async def test_a_region_and_a_site_sharing_a_slug_do_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NetBox slugs are unique per model, not globally.

    A region and a site may both be "gondor". The platform keys sites on
    (provider_id, external_id), so without the prefix the two would be one row --
    a site that is its own parent, or an aggregation node with a device on it.
    """
    provider = _provider(token="netbox-token")
    _hierarchy_transport(
        monkeypatch,
        sites={
            "results": [
                {
                    "id": 1,
                    "slug": "gondor",
                    "name": "Gondor DC",
                    "region": {"id": 20, "slug": "gondor", "name": "Gondor"},
                }
            ],
            "next": None,
        },
    )

    listed = await _listed(provider)
    ids = [site.external_id for site in listed]

    assert len(ids) == len(set(ids)), f"duplicate external ids: {ids}"
    assert {"gondor", "region:gondor"} <= set(ids)
    site = next(one for one in listed if one.external_id == "gondor")
    assert site.parent_external_id == "region:gondor"
    assert site.parent_external_id != site.external_id


@pytest.mark.asyncio
async def test_get_site_resolves_a_hierarchy_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening a region row must work, not 404.

    The nodes are sites as far as the platform is concerned, so every read path
    has to know the prefix -- not only the one that created them.
    """
    provider = _provider(token="netbox-token")
    _hierarchy_transport(monkeypatch)

    node = await provider.get(
        ResourceType.SITE,
        "region:gondor",
        context=ProviderCallContext(provider_id="netbox:primary", operation="get_site"),
    )

    # get() returns a union; narrow it the way the other site tests here do.
    assert isinstance(node, SiteRef)
    assert node.external_id == "region:gondor"
    assert node.name == "Gondor"
    assert node.tags == {"node_type": "region"}


@pytest.mark.asyncio
async def test_a_cyclic_hierarchy_does_not_hang_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk climbs data from a remote system, so it stops at the first repeat.

    NetBox's own tree cannot contain a cycle, but a sync that hangs is far worse
    than one that fails: it holds the provider's claim and blocks every later
    tick, where a failure is retried.
    """
    provider = _provider(token="netbox-token")
    _install_transport(
        monkeypatch,
        _json_transport(
            {
                "/api/dcim/sites/": {
                    "results": [
                        {
                            "id": 1,
                            "slug": "mt1",
                            "name": "Minas Tirith",
                            "region": {"id": 20, "slug": "a", "name": "A"},
                        }
                    ],
                    "next": None,
                },
                "/api/dcim/regions/": {
                    "results": [
                        {
                            "id": 20,
                            "slug": "a",
                            "name": "A",
                            "parent": {"id": 21, "slug": "b", "name": "B"},
                        },
                        {
                            "id": 21,
                            "slug": "b",
                            "name": "B",
                            "parent": {"id": 20, "slug": "a", "name": "A"},
                        },
                    ],
                    "next": None,
                },
            }
        ),
    )

    listed = await _listed(provider)

    assert {"region:a", "region:b"} <= {site.external_id for site in listed}


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
    assert result.safe_details["netbox_families_undeclared"] > 0
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
