# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Infrahub inventory provider HTTP error semantics."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from hegemony_inventory_infrahub import provider as infrahub_provider_module
from hegemony_inventory_infrahub.config import InfrahubProviderConfig
from hegemony_inventory_infrahub.provider import InfrahubInventoryProvider
from hegemony_inventory_sdk import (
    DeviceDescriptor,
    InventoryProviderError,
    ProviderCallContext,
    ProviderErrorCode,
    ResourceType,
    SiteRef,
)
from tests.inventory_fakes import FakePlatformServices


def _provider(
    *, token: str | None = None, config: dict[str, Any] | None = None
) -> InfrahubInventoryProvider:
    return InfrahubInventoryProvider(
        provider_id="infrahub:primary",
        config={
            "url": "https://infrahub.example",
            "token_ref": "{{ secret('vault://inventory/infrahub/token') }}",
            **(config or {}),
        },
        services=FakePlatformServices(
            token=token, provider_id="infrahub:primary", provider_type="infrahub"
        ),
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.MockTransport,
) -> None:
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=handler, **kwargs)

    monkeypatch.setattr(infrahub_provider_module.httpx, "AsyncClient", client_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_token", "expected_authorization", "expected_x_infrahub_key"),
    [
        ("infrahub-token", None, "infrahub-token"),
        ("  infrahub-token  ", None, "infrahub-token"),
        ("Bearer infrahub-token", "Bearer infrahub-token", None),
    ],
)
async def test_infrahub_token_header_selection(
    monkeypatch: pytest.MonkeyPatch,
    resolved_token: str,
    expected_authorization: str | None,
    expected_x_infrahub_key: str | None,
) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["x_infrahub_key"] = request.headers.get("X-INFRAHUB-KEY")
        return httpx.Response(200, json={"data": {"__typename": "Query"}})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token=resolved_token)
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert captured == {
        "authorization": expected_authorization,
        "x_infrahub_key": expected_x_infrahub_key,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (404, ProviderErrorCode.NOT_FOUND),
        (400, ProviderErrorCode.MALFORMED_QUERY),
    ],
)
async def test_infrahub_maps_client_errors_before_raise_for_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: ProviderErrorCode,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "private provider payload"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")

    with pytest.raises(InventoryProviderError) as exc_info:
        await provider._graphql(
            "query { __typename }",
            {},
            ProviderCallContext(
                provider_id="infrahub:primary",
                operation="query_devices",
                query_hash="client-error-test",
                config_version=1,
            ),
        )

    assert exc_info.value.envelope.code == expected_code
    assert "private provider payload" not in exc_info.value.envelope.message


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_infrahub_auth_failures_include_safe_http_details(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "private provider payload"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")

    with pytest.raises(InventoryProviderError) as exc_info:
        await provider._graphql(
            "query { __typename }",
            {},
            ProviderCallContext(
                provider_id="infrahub:primary",
                operation="auto_sync_sites",
                query_hash="auth-failure-test",
                config_version=1,
            ),
        )

    envelope = exc_info.value.envelope
    assert envelope.code == ProviderErrorCode.AUTH_FAILED
    assert envelope.message == "Infrahub authentication failed"
    assert envelope.safe_details == {
        "auth_header_name": "Authorization",
        "auth_attempt": "Authorization: Token",
        "auth_attempts": [
            "X-INFRAHUB-KEY",
            "Authorization: Bearer",
            "Authorization: Token",
        ],
        "host": "infrahub.example",
        "status_code": status_code,
    }
    assert "private provider payload" not in envelope.message


@pytest.mark.asyncio
async def test_infrahub_graphql_failures_include_safe_graphql_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "Cannot query field 'platform' on type 'InfraDevice'.",
                        "path": ["InfraDevice", "edges", 0, "node", "platform"],
                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                    }
                ]
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")

    with pytest.raises(InventoryProviderError) as exc_info:
        await provider._graphql(
            "query { broken }",
            {},
            ProviderCallContext(
                provider_id="infrahub:primary",
                operation="auto_sync_sites",
                query_hash="graphql-error-test",
                config_version=1,
            ),
        )

    envelope = exc_info.value.envelope
    assert envelope.code == ProviderErrorCode.MALFORMED_QUERY
    assert envelope.message == "Infrahub GraphQL query failed"
    assert envelope.safe_details == {
        "graphql_errors": [
            {
                "message": "Cannot query field 'platform' on type 'InfraDevice'.",
                "path": ["InfraDevice", "edges", "0", "node", "platform"],
                "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
            }
        ]
    }


@pytest.mark.asyncio
async def test_infrahub_raw_token_falls_back_to_bearer_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(
            {
                "authorization": request.headers.get("Authorization"),
                "x_infrahub_key": request.headers.get("X-INFRAHUB-KEY"),
            }
        )
        if request.headers.get("X-INFRAHUB-KEY") == "infrahub-token":
            return httpx.Response(401, json={"detail": "wrong header for this deployment"})
        if request.headers.get("Authorization") == "Bearer infrahub-token":
            return httpx.Response(200, json={"data": {"__typename": "Query"}})
        return httpx.Response(401, json={"detail": "unexpected auth"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert seen_headers == [
        {"authorization": None, "x_infrahub_key": "infrahub-token"},
        {"authorization": "Bearer infrahub-token", "x_infrahub_key": None},
    ]


@pytest.mark.asyncio
async def test_infrahub_bearer_token_falls_back_to_x_infrahub_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(
            {
                "authorization": request.headers.get("Authorization"),
                "x_infrahub_key": request.headers.get("X-INFRAHUB-KEY"),
            }
        )
        if request.headers.get("Authorization") == "Bearer infrahub-token":
            return httpx.Response(401, json={"detail": "wrong header for this deployment"})
        if request.headers.get("X-INFRAHUB-KEY") == "infrahub-token":
            return httpx.Response(200, json={"data": {"__typename": "Query"}})
        return httpx.Response(401, json={"detail": "unexpected auth"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="Bearer infrahub-token")
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert seen_headers == [
        {"authorization": "Bearer infrahub-token", "x_infrahub_key": None},
        {"authorization": None, "x_infrahub_key": "infrahub-token"},
    ]


@pytest.mark.asyncio
async def test_infrahub_raw_token_uses_x_infrahub_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["x_infrahub_key"] = request.headers.get("X-INFRAHUB-KEY")
        return httpx.Response(200, json={"data": {"__typename": "Query"}})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert captured == {
        "authorization": None,
        "x_infrahub_key": "infrahub-token",
    }


@pytest.mark.asyncio
async def test_infrahub_prefixed_token_uses_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["x_infrahub_key"] = request.headers.get("X-INFRAHUB-KEY")
        return httpx.Response(200, json={"data": {"__typename": "Query"}})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="Bearer infrahub-token")
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert captured == {
        "authorization": "Bearer infrahub-token",
        "x_infrahub_key": None,
    }


@pytest.mark.asyncio
async def test_infrahub_empty_resolved_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(token=None)
    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is False
    assert result.message == "Infrahub API token resolved to an empty value"
    assert result.error is not None
    assert result.error["code"] == ProviderErrorCode.SCHEMA_VALIDATION_FAILED.value


@pytest.mark.asyncio
async def test_infrahub_get_filters_requested_external_id(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    descriptors = [
        DeviceDescriptor(
            provider_id=provider.id,
            external_id="first",
            name="first",
            mgmt_host="192.0.2.10",
            platform="ios-xe",
        ),
        DeviceDescriptor(
            provider_id=provider.id,
            external_id="requested",
            name="requested",
            mgmt_host="192.0.2.11",
            platform="ios-xe",
        ),
    ]

    async def fake_query_devices(*_args: Any, **_kwargs: Any) -> list[DeviceDescriptor]:
        return descriptors

    monkeypatch.setattr(provider, "query_devices", fake_query_devices)

    result = await provider.get(
        resource_type=ResourceType.DEVICE,
        external_id="requested",
        context=ProviderCallContext(provider_id=provider.id, operation="get"),
    )

    assert isinstance(result, DeviceDescriptor)
    assert result.external_id == "requested"


@pytest.mark.asyncio
async def test_infrahub_lists_site_refs_as_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()

    async def fake_list_sites(*_args: Any, **_kwargs: Any) -> list[SiteRef]:
        return [SiteRef(provider_id=provider.id, external_id="site-1", name="Site 1")]

    monkeypatch.setattr(provider, "list_sites", fake_list_sites)

    result = await provider.list(
        ResourceType.SITE,
        context=ProviderCallContext(provider_id=provider.id, operation="list"),
    )

    assert result[0].external_id == "site-1"
    assert result[0].display_name == "Site 1"


@pytest.mark.asyncio
async def test_infrahub_get_filters_requested_site_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()

    async def fake_list_sites(*_args: Any, **_kwargs: Any) -> list[SiteRef]:
        return [
            SiteRef(provider_id=provider.id, external_id="site-a", name="Site A"),
            SiteRef(provider_id=provider.id, external_id="site-b", name="Site B"),
        ]

    monkeypatch.setattr(provider, "list_sites", fake_list_sites)

    result = await provider.get(
        resource_type=ResourceType.SITE,
        external_id="site-b",
        context=ProviderCallContext(provider_id=provider.id, operation="get"),
    )

    assert isinstance(result, SiteRef)
    assert result.external_id == "site-b"


def test_infrahub_config_rejects_blank_branch_and_device_kind() -> None:
    base_config = {"url": "https://infrahub.example", "token_ref": "vault://token"}

    with pytest.raises(ValidationError, match="token_ref is required"):
        InfrahubProviderConfig.model_validate({**base_config, "token_ref": "   "})

    with pytest.raises(ValidationError, match="value is required"):
        InfrahubProviderConfig.model_validate({**base_config, "branch": "   "})

    with pytest.raises(ValidationError, match="value is required"):
        InfrahubProviderConfig.model_validate({**base_config, "device_kind": "   "})


def test_infrahub_config_requires_token_ref() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        InfrahubProviderConfig.model_validate({"url": "https://infrahub.example"})


@pytest.mark.asyncio
async def test_infrahub_connection_probe_uses_no_unused_branch_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"__typename": "Query"}})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")

    result = await provider.test_connection(
        context=ProviderCallContext(provider_id=provider.id, operation="test_connection")
    )

    assert result.ok is True
    assert "query HegemonyInventoryDevices" in captured["query"]
    assert "limit: 1" in captured["query"]
    assert "branch:" not in captured["query"]
    assert "first:" not in captured["query"]
    assert captured["variables"] == {}


@pytest.mark.asyncio
async def test_infrahub_query_devices_uses_branch_endpoint_limit_and_relationship_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["raw_path"] = request.url.raw_path.decode()
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "InfraDevice": {
                        "edges": [
                            {
                                "node": {
                                    "id": "device-1",
                                    "display_label": "atl1-edge1",
                                    "name": {"value": "atl1-edge1"},
                                    "primary_address": {
                                        "node": {"address": {"value": "192.0.2.10/32"}}
                                    },
                                    "platform": {"node": {"name": {"value": "ios-xe"}}},
                                    "role": {"value": "edge"},
                                    "site": {"node": {"name": {"value": "ATL1"}}},
                                    "tags": {"edges": [{"node": {"name": {"value": "core"}}}]},
                                }
                            }
                        ]
                    }
                }
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = InfrahubInventoryProvider(
        provider_id="infrahub:primary",
        config={
            "url": "https://infrahub.example",
            "token_ref": "{{ secret('vault://inventory/infrahub/token') }}",
            "branch": "feature/demo",
        },
        services=FakePlatformServices(
            token="infrahub-token", provider_id="infrahub:primary", provider_type="infrahub"
        ),
    )

    devices = await provider.query_devices(
        "",
        limit=1,
        context=ProviderCallContext(
            provider_id=provider.id,
            operation="auto_sync_devices",
            query_hash="device-query-shape-test",
            config_version=1,
        ),
    )

    assert captured["path"] == "/graphql/feature/demo"
    assert captured["raw_path"] == "/graphql/feature%2Fdemo"
    assert "branch:" not in captured["query"]
    assert "first:" not in captured["query"]
    assert "limit: 1" in captured["query"]
    assert "primary_address { node { address { value } } }" in captured["query"]
    assert "platform { node { name { value } } }" in captured["query"]
    assert "site { node { name { value } } }" in captured["query"]
    assert "tags { edges { node { name { value } } } }" in captured["query"]
    assert "mgmt_port" not in captured["query"]
    assert "hegemony_ssh_username_ref" not in captured["query"]
    assert captured["variables"] == {}
    # Infrahub provided no mgmt_port; the descriptor leaves it unset and the core
    # inventory service fills the default during materialization.
    assert devices[0].mgmt_port is None
    assert devices[0].site is not None
    assert devices[0].site.name == "ATL1"
    assert devices[0].native_tags == ("core",)


@pytest.mark.asyncio
async def test_infrahub_merges_default_access_config_with_mapped_device_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "InfraDevice": {
                        "edges": [
                            {
                                "node": {
                                    "id": "device-1",
                                    "display_label": "atl1-edge1",
                                    "name": {"value": "atl1-edge1"},
                                    "primary_address": {
                                        "node": {"address": {"value": "192.0.2.10/32"}}
                                    },
                                    "platform": {"node": {"name": {"value": "ios-xe"}}},
                                    "role": {"value": "edge"},
                                    "site": {"node": {"name": {"value": "ATL1"}}},
                                    "tags": {"edges": []},
                                    "hegemony_ssh_username_ref": {
                                        "value": "{{ secret('vault://devices/atl1-edge1/user') }}"
                                    },
                                    "hegemony_enable_password_ref": {
                                        "value": "{{ secret('vault://devices/atl1-edge1/enable') }}"
                                    },
                                }
                            }
                        ]
                    }
                }
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(
        token="infrahub-token",
        config={
            "field_map": {
                "access_config.ssh.username_ref": "hegemony_ssh_username_ref.value",
                "access_config.enable.password_ref": "hegemony_enable_password_ref.value",
            },
            "default_access_config": {
                "ssh": {
                    "password_ref": "{{ secret('vault://shared/infrahub/password') }}",
                    "private_key_ref": "{{ secret('vault://shared/infrahub/key') }}",
                }
            },
        },
    )

    devices = await provider.query_devices(
        "",
        limit=1,
        context=ProviderCallContext(
            provider_id=provider.id,
            operation="auto_sync_devices",
            query_hash="default-access-config-test",
            config_version=1,
        ),
    )

    assert "hegemony_ssh_username_ref { value }" in captured["query"]
    assert "hegemony_enable_password_ref { value }" in captured["query"]
    assert devices[0].access_config == {
        "ssh": {
            "username_ref": "{{ secret('vault://devices/atl1-edge1/user') }}",
            "password_ref": "{{ secret('vault://shared/infrahub/password') }}",
            "private_key_ref": "{{ secret('vault://shared/infrahub/key') }}",
        },
        "enable": {
            "password_ref": "{{ secret('vault://devices/atl1-edge1/enable') }}",
        },
    }


@pytest.mark.asyncio
async def test_infrahub_list_sites_uses_site_only_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "InfraDevice": {
                        "edges": [
                            {"node": {"site": {"node": {"name": {"value": "ATL1"}}}}},
                            {"node": {"site": {"node": {"name": {"value": "ATL1"}}}}},
                            {"node": {"site": {"node": {"name": {"value": "ATL2"}}}}},
                        ]
                    }
                }
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(token="infrahub-token")
    sites = await provider.list_sites(
        limit=5,
        context=ProviderCallContext(
            provider_id=provider.id,
            operation="auto_sync_sites",
            query_hash="site-query-shape-test",
            config_version=1,
        ),
    )

    assert [site.external_id for site in sites] == ["ATL1", "ATL2"]
    assert "query HegemonyInventorySites" in captured["query"]
    assert "site { node { name { value } } }" in captured["query"]
    assert "primary_address" not in captured["query"]
    assert "platform" not in captured["query"]
    assert "tags" not in captured["query"]


def test_infrahub_config_normalizes_field_map_entries() -> None:
    config = InfrahubProviderConfig.model_validate(
        {
            "url": "https://infrahub.example",
            "token_ref": "vault://token",
            "field_map": {
                " platform ": " platform.node.name.value ",
                "access_config.ssh.username_ref": "  hegemony_ssh_username_ref.value  ",
                "": "name.value",
            },
        }
    )

    assert config.field_map == {
        "platform": "platform.node.name.value",
        "access_config.ssh.username_ref": "hegemony_ssh_username_ref.value",
    }


@pytest.mark.asyncio
async def test_infrahub_merges_default_jump_host_onto_every_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet-wide bastion in default_access_config reaches every device.

    Like NetBox and unlike the git provider, Infrahub offers no per-device
    jump_host mapping — the mappable access_config paths are the ssh/enable
    credential refs — so the provider default is the whole contract, and it
    must survive the field-map merge that builds mapped_access_config.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "InfraDevice": {
                        "edges": [
                            {
                                "node": {
                                    "id": "device-1",
                                    "display_label": "atl1-edge1",
                                    "name": {"value": "atl1-edge1"},
                                    "primary_address": {
                                        "node": {"address": {"value": "192.0.2.10/32"}}
                                    },
                                    "platform": {"node": {"name": {"value": "ios-xe"}}},
                                    "role": {"value": "edge"},
                                    "site": {"node": {"name": {"value": "ATL1"}}},
                                    "tags": {"edges": []},
                                    "hegemony_ssh_username_ref": {
                                        "value": "{{ secret('vault://devices/atl1-edge1/user') }}"
                                    },
                                }
                            }
                        ]
                    }
                }
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(
        token="infrahub-token",
        config={
            "field_map": {
                "access_config.ssh.username_ref": "hegemony_ssh_username_ref.value",
            },
            "default_access_config": {
                "ssh": {"private_key_ref": "{{ secret('vault://shared/infrahub/key') }}"},
                "jump_host": {
                    "host": "dind",
                    "port": 2222,
                    "username_ref": "{{ secret('vault://lab/bastion/username') }}",
                    "password_ref": "{{ secret('vault://lab/bastion/password') }}",
                },
            },
        },
    )

    devices = await provider.query_devices(
        "",
        limit=1,
        context=ProviderCallContext(
            provider_id=provider.id,
            operation="auto_sync_devices",
            query_hash="default-jump-host-test",
            config_version=1,
        ),
    )

    assert devices[0].access_config["jump_host"] == {
        "host": "dind",
        "port": 2222,
        "username_ref": "{{ secret('vault://lab/bastion/username') }}",
        "password_ref": "{{ secret('vault://lab/bastion/password') }}",
    }
    # The scope is additive: mapped and defaulted ssh refs still merge.
    assert devices[0].access_config["ssh"] == {
        "username_ref": "{{ secret('vault://devices/atl1-edge1/user') }}",
        "private_key_ref": "{{ secret('vault://shared/infrahub/key') }}",
    }


@pytest.mark.asyncio
async def test_infrahub_without_default_jump_host_emits_no_jump_host_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured bastion means no scope at all, not an empty one.

    An empty ``jump_host`` block would reach the worker's resolver, which
    treats the block's presence as "a bastion was declared" and fails the
    connection rather than dialling the device directly. The parity test for
    NetBox is test_netbox_without_default_jump_host_emits_no_jump_host_scope;
    this had to be its own provider instance, because asserting it inside the
    configured-jump_host test above can only ever pass vacuously.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "InfraDevice": {
                        "edges": [
                            {
                                "node": {
                                    "id": "device-1",
                                    "display_label": "atl1-edge1",
                                    "name": {"value": "atl1-edge1"},
                                    "primary_address": {
                                        "node": {"address": {"value": "192.0.2.10/32"}}
                                    },
                                    "platform": {"node": {"name": {"value": "ios-xe"}}},
                                    "role": {"value": "edge"},
                                    "site": {"node": {"name": {"value": "ATL1"}}},
                                    "tags": {"edges": []},
                                    "hegemony_ssh_username_ref": {
                                        "value": "{{ secret('vault://devices/atl1-edge1/user') }}"
                                    },
                                }
                            }
                        ]
                    }
                }
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    provider = _provider(
        token="infrahub-token",
        config={
            "field_map": {
                "access_config.ssh.username_ref": "hegemony_ssh_username_ref.value",
            },
            "default_access_config": {
                "ssh": {"private_key_ref": "{{ secret('vault://shared/infrahub/key') }}"},
            },
        },
    )

    devices = await provider.query_devices(
        "",
        limit=1,
        context=ProviderCallContext(
            provider_id=provider.id,
            operation="auto_sync_devices",
            query_hash="no-jump-host-test",
            config_version=1,
        ),
    )

    assert "jump_host" not in devices[0].access_config
    # The other scopes are unaffected by the absent one.
    assert devices[0].access_config["ssh"] == {
        "username_ref": "{{ secret('vault://devices/atl1-edge1/user') }}",
        "private_key_ref": "{{ secret('vault://shared/infrahub/key') }}",
    }
