# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider config schema."""

from __future__ import annotations

import ast
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from hegemony_inventory_sdk import MaintenanceState, validate_object_type_id

from .filters import LOOKUP_SUFFIX, intersect_queries

# NetBox device statuses -> the platform's MaintenanceState. "Gone or going" is
# disabled; "not yet in service" is maintenance. Anything unlisted (including
# "active") leaves the device active. Override per provider with ``status_map``.
DEFAULT_STATUS_MAP = {
    "active": "active",
    "offline": "disabled",
    "decommissioning": "disabled",
    "planned": "maintenance",
    "staged": "maintenance",
    "failed": "maintenance",
    "inventory": "maintenance",
}

DEFAULT_FIELD_MAP = {
    "external_id": "id",
    "name": "name",
    "display_name": "display",
    "hostname": "hostname",
    "mgmt_host": "primary_ip4.address",
    "platform": "platform.slug",
    "model": "device_type.model",
    "vendor": "device_type.manufacturer.name",
    # Surfaced under ``descriptive`` — the platform's device row has no column
    # for these, but they are searchable there and visible over the API.
    "serial": "serial",
    "asset_tag": "asset_tag",
    "rack": "rack.name",
    "position": "position",
    "tenant": "tenant.slug",
    "location": "location.slug",
    "mgmt_host_v6": "primary_ip6.address",
    "site.external_id": "site.slug",
    "site.name": "site.name",
    "role": "role.slug",
    "tags": "tags",
    "access_config.ssh.username_ref": "custom_fields.hegemony_ssh_username_ref",
    "access_config.ssh.password_ref": "custom_fields.hegemony_ssh_password_ref",
    "access_config.enable.password_ref": "custom_fields.hegemony_enable_password_ref",
    "access_config.ssh.private_key_ref": "custom_fields.hegemony_ssh_private_key_ref",
}


class NetBoxScopeClause(BaseModel):
    """One visible NetBox filter rule."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*(?:__[A-Za-z0-9_]+)?$",
        description="NetBox filter field, as advertised by this family's query schema.",
    )
    lookup: Literal[
        "exact",
        "not",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "gt",
        "gte",
        "lt",
        "lte",
        "empty",
    ] = "exact"
    schema_validation: Literal["required", "legacy"] = "required"
    value: str | list[str] = Field(..., max_length=2048)

    @field_validator("field")
    @classmethod
    def strip_scope_clause_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("scope clause field and value are required")
        return stripped

    @model_validator(mode="after")
    def validate_values(self) -> NetBoxScopeClause:
        values = self.value if isinstance(self.value, list) else [self.value]
        if not values or any(len(value) > 2048 for value in values):
            raise ValueError("filter values are required and must be at most 2048 characters")
        if self.schema_validation == "required":
            if any(not value.strip() for value in values):
                raise ValueError("filter values are required")
            self.value = (
                [v.strip() for v in values] if isinstance(self.value, list) else self.value.strip()
            )
        return self


class NetBoxInventoryFilter(BaseModel):
    """The single tenant mapping and rule set for one provider configuration."""

    model_config = ConfigDict(extra="forbid")

    tenant_slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description=(
            "Optional NetBox tenant slug. It automatically scopes the tenant family by "
            "slug and every family whose discovered schema exposes a tenant filter."
        ),
    )
    queries: dict[str, list[NetBoxScopeClause]] = Field(
        default_factory=dict,
        description="Additional typed filters keyed by inventory object type.",
    )
    tenant_scope_exemptions: list[str] = Field(
        default_factory=list,
        max_length=128,
        description=(
            "Families intentionally allowed when they do not advertise a tenant filter. "
            "Use only for shared reference data or when the NetBox token enforces the boundary."
        ),
    )

    @field_validator("queries")
    @classmethod
    def validate_scope_queries(
        cls, value: dict[str, list[NetBoxScopeClause]]
    ) -> dict[str, list[NetBoxScopeClause]]:
        normalized: dict[str, list[NetBoxScopeClause]] = {}
        invalid: list[str] = []
        for object_type, clauses in value.items():
            key = str(object_type).strip()
            try:
                validate_object_type_id(key)
            except ValueError:
                invalid.append(key)
                continue
            if clauses:
                normalized[key] = clauses
        if invalid:
            raise ValueError(
                f"scope queries have invalid object type id(s): {', '.join(sorted(invalid))}"
            )
        return normalized

    @field_validator("tenant_scope_exemptions")
    @classmethod
    def validate_tenant_scope_exemptions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        invalid: list[str] = []
        for object_type in value:
            key = object_type.strip()
            try:
                validate_object_type_id(key)
            except ValueError:
                invalid.append(key)
                continue
            if key not in normalized:
                normalized.append(key)
        if invalid:
            raise ValueError(
                "tenant scope exemptions have invalid object type id(s): "
                f"{', '.join(sorted(invalid))}"
            )
        return normalized


class _LegacyInventoryScope(NetBoxInventoryFilter):
    """Read-only compatibility model, never exposed in the new configuration schema."""

    name: str = Field(..., min_length=1, max_length=100)


class NetBoxProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    token_ref: str = Field(
        ...,
        description=(
            "Reference to a stored secret holding the NetBox API token, "
            "e.g. {{ secret('vault://netbox/token') }}."
        ),
        json_schema_extra={"x_secret_ref": True},
    )
    auth_scheme: str = Field(
        "Bearer",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="Primary HTTP Authorization scheme prepended to resolved token values.",
    )
    auth_fallback_schemes: list[str] = Field(
        default_factory=lambda: ["Token"],
        max_length=4,
        description="Fallback Authorization schemes retried after auth failures when the token is unprefixed.",
    )
    verify_tls: bool = True
    schema_discovery: bool = Field(
        False,
        description=(
            "Have the connection test also ask this NetBox what it serves. Every NetBox "
            "publishes an OpenAPI description of its own API at /api/schema/, generated "
            "from the models it is actually running, and it is the only way to learn what "
            "THIS instance has: which of the offered object types it serves, what its "
            "plugins add, and which custom fields are defined on it. Off by default "
            "because that document is several megabytes on a stock install, where a "
            "connection test is otherwise a few hundred bytes. This option affects only the connection test: a "
            "failure to fetch or parse it leaves the connection test's own result alone."
        ),
    )
    site_tree_source: Literal["region", "site_group", "none"] = Field(
        "region",
        description=(
            "Which NetBox hierarchy becomes the platform's site tree. NetBox keeps "
            "TWO independent nested hierarchies above a site -- Region (geography) "
            "and Site Group (function) -- while a platform site has a single parent, "
            "so one of them has to be chosen. Regardless of the choice, BOTH are "
            "always surfaced as site tags ('region' and 'group'), so picking one "
            "never loses the other: the unchosen axis stays available for filtering. "
            "'none' reproduces the flat list this provider produced before the tree "
            "was mapped at all."
        ),
    )
    tag_key_prefix: str = Field(
        "tag:",
        max_length=32,
        description=(
            "Prefix for NetBox tag slugs surfaced as filterable device tags, e.g. the "
            "NetBox tag 'core' becomes tags['tag:core'] = 'true'. The prefix keeps a "
            "NetBox tag named 'role' from colliding with the mapped device role; set it "
            "to an empty string to map NetBox tags onto bare keys."
        ),
    )
    status_map: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_STATUS_MAP),
        description=(
            "Maps a NetBox device status slug to a maintenance state "
            "(active, maintenance or disabled). Statuses left out stay active."
        ),
    )
    field_map: dict[str, str] = Field(
        default_factory=lambda: {
            key: value
            for key, value in DEFAULT_FIELD_MAP.items()
            if key.startswith("access_config.")
        },
        description="Credential-reference paths. Legacy non-credential defaults are ignored.",
    )
    default_access_config: dict[str, Any] = Field(default_factory=dict)
    inventory_filter: NetBoxInventoryFilter = Field(
        default_factory=NetBoxInventoryFilter,
        description="Tenant mapping and additional filters for this provider instance.",
    )
    discovery_url: HttpUrl | None = Field(
        default=None,
        description="Canonical NetBox base URL used to fetch shared API schemas when connections use different aliases for the same instance. Defaults to the connection URL.",
    )
    public_url: HttpUrl | None = Field(
        default=None,
        description="Browser-accessible NetBox base URL for source links; defaults to the API connection URL.",
    )
    page_size: int = Field(
        100,
        ge=1,
        le=1000,
        description="Number of NetBox records requested per API page.",
    )
    timeout_seconds: float = Field(10.0, gt=0, le=120)
    max_response_bytes: int = Field(
        64 * 1024 * 1024,
        ge=64 * 1024,
        le=512 * 1024 * 1024,
        description=(
            "Largest response body this provider will read, in bytes. A timeout bounds "
            "how LONG a read may take and nothing bounded how MUCH it may return, so a "
            "single response could spend the API process's memory while never once "
            "going quiet. The default is generous because /api/schema/ is genuinely "
            "several megabytes; it is a ceiling on the absurd, not a budget. A body "
            "that reaches it is abandoned unparsed."
        ),
    )

    @field_validator("token_ref")
    @classmethod
    def token_ref_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("token_ref is required")
        value = value.strip()
        # A reference is exported verbatim. Never accept a literal credential here.
        # Normalize historical URI references into the template the host resolves.
        if "://" in value and not value.startswith("{{"):
            scheme, _, path = value.partition("://")
            if (
                scheme.replace("_", "").replace("-", "").isalnum()
                and path
                and not any(c.isspace() for c in value)
            ):
                value = "{{ secret(" + repr(value) + ") }}"
        if not (value.startswith("{{") and value.endswith("}}")):
            raise ValueError("token_ref must be a secret(), env(), or file() reference")
        try:
            expression = ast.parse(value[2:-2].strip(), mode="eval").body
        except SyntaxError as exc:
            raise ValueError("token_ref must be a valid secret reference") from exc
        if not (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id in {"secret", "env", "file"}
            and len(expression.args) == 1
            and isinstance(expression.args[0], ast.Constant)
            and isinstance(expression.args[0].value, str)
            and expression.args[0].value.strip()
            and not expression.keywords
        ):
            raise ValueError("token_ref must be a secret(), env(), or file() reference")
        return value

    @field_validator("auth_scheme", mode="before")
    @classmethod
    def auth_scheme_required(cls, value: Any) -> str:
        if value is None:
            return "Bearer"
        stripped = str(value).strip()
        if not stripped:
            raise ValueError("auth_scheme is required")
        return stripped

    @field_validator("auth_fallback_schemes", mode="before")
    @classmethod
    def normalize_auth_fallback_schemes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            candidates = [item.strip() for item in value.split(",")]
        elif isinstance(value, list):
            candidates = [str(item).strip() for item in value]
        else:
            raise ValueError("auth_fallback_schemes must be a list or comma-separated string")
        return [item for item in candidates if item]

    @field_validator("tag_key_prefix", mode="before")
    @classmethod
    def normalize_tag_key_prefix(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="before")
    @classmethod
    def drop_retired_object_types(cls, value: Any) -> Any:
        """Ignore a stored ``object_types``, which briefly duplicated a platform control.

        This config once carried a ceiling on which families the provider could read.
        The platform asks the same question on the provider form, and two controls for
        one choice is how a ticked object type came to sync nothing, so the ceiling is
        gone. A config written while it existed still has to validate: ``extra="forbid"``
        would otherwise reject it outright rather than ignore a key that no longer means
        anything.
        """
        if isinstance(value, dict):
            value = {
                key: item
                for key, item in value.items()
                if key not in {"object_types", "query_cache_ttl_seconds"}
            }
        return value

    @model_validator(mode="before")
    @classmethod
    def migrate_inventory_filters(cls, value: Any) -> Any:
        """Flatten the active legacy scope and raw restrictions without widening reads.

        Inactive presets never affected reads and are retired. Conflicting restrictions
        remain errors. Repeated values within one raw query retain their OR semantics.
        """
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if not any(
            key in value
            for key in (
                "inventory_scopes",
                "active_inventory_scope",
                "object_queries",
                "device_query",
            )
        ):
            return value
        scopes = [
            _LegacyInventoryScope.model_validate(item).model_dump(mode="json")
            for item in (value.pop("inventory_scopes", []) or [])
        ]
        names = [item["name"] for item in scopes]
        if len(names) != len(set(names)):
            raise ValueError("inventory scope names must be unique")
        selected = value.pop("active_inventory_scope", None)
        legacy = next((s for s in scopes if s.get("name") == selected), None)
        if selected and legacy is None:
            raise ValueError("active_inventory_scope must name one of inventory_scopes")
        current = NetBoxInventoryFilter.model_validate(
            value.get("inventory_filter") or {}
        ).model_dump(mode="json", exclude_unset=True)
        if legacy:
            for key in ("tenant_slug", "tenant_scope_exemptions"):
                old = legacy.get(key)
                if key in current and current[key] != old:
                    raise ValueError(f"Conflicting old and new inventory filter {key}")
                if old is not None:
                    current[key] = old
        raw_queries = value.pop("object_queries", {}) or {}
        if not isinstance(raw_queries, dict) or any(
            not isinstance(family, str) or not isinstance(query, str)
            for family, query in raw_queries.items()
        ):
            raise ValueError("object_queries must map object types to query strings")
        queries: dict[str, str] = dict(raw_queries)
        device_query = value.pop("device_query", "") or ""
        if not isinstance(device_query, str):
            raise ValueError("device_query must be a query string")
        if device_query:
            queries["device"] = intersect_queries(queries.get("device", ""), device_query)
        legacy_parameters = {
            family: set(parse_qs(query.lstrip("?"), keep_blank_values=True))
            for family, query in queries.items()
        }
        for rules in ((legacy or {}).get("queries", {}), current.get("queries", {})):
            for family, clauses in rules.items():
                for clause in clauses:
                    param = clause["field"] + LOOKUP_SUFFIX[clause.get("lookup", "exact")]
                    queries[family] = intersect_queries(
                        queries.get(family, ""), urlencode({param: clause["value"]}, doseq=True)
                    )
        current["queries"] = {
            family: [
                {
                    "field": field,
                    "lookup": "exact",
                    "value": values[0] if len(values) == 1 else values,
                    "schema_validation": "legacy"
                    if field in legacy_parameters.get(family, set())
                    else "required",
                }
                for field, values in parse_qs(query.lstrip("?"), keep_blank_values=True).items()
            ]
            for family, query in queries.items()
            if query
        }
        value["inventory_filter"] = current
        return value

    @field_validator("status_map")
    @classmethod
    def validate_status_map(cls, value: dict[str, str]) -> dict[str, str]:
        """Normalize keys/values and reject states the platform does not model."""
        allowed = {state.value for state in MaintenanceState}
        normalized: dict[str, str] = {}
        for status, state in value.items():
            if not isinstance(status, str) or not isinstance(state, str):
                continue
            status_key = status.strip().lower()
            state_value = state.strip().lower()
            if not status_key or not state_value:
                continue
            if state_value not in allowed:
                raise ValueError(
                    f"status_map['{status_key}'] must be one of {sorted(allowed)}, "
                    f"got '{state_value}'"
                )
            normalized[status_key] = state_value
        return normalized

    @field_validator("field_map")
    @classmethod
    def normalize_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, path in value.items():
            if not isinstance(key, str) or not isinstance(path, str):
                continue
            stripped_key = key.strip()
            stripped_path = path.strip()
            if not stripped_key or not stripped_path:
                continue
            if stripped_key not in DEFAULT_FIELD_MAP:
                raise ValueError(f"field_map has unknown key: {stripped_key}")
            if not stripped_key.startswith("access_config."):
                if stripped_path != DEFAULT_FIELD_MAP[stripped_key]:
                    raise ValueError(
                        f"field_map.{stripped_key} cannot be overridden; projection "
                        "reads the normalized object attribute"
                    )
                # Accept historical full defaults without retaining dead controls.
                continue
            normalized[stripped_key] = stripped_path
        return normalized

    def safe_config(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
