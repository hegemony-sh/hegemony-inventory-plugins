# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""NetBox inventory provider config schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from hegemony_inventory_sdk import MaintenanceState, validate_object_type_id

# NetBox device statuses -> the platform's MaintenanceState. "Gone or going" is
# disabled; "not yet in service" is maintenance. Anything unlisted (including
# "active") leaves the device active. Override per provider with ``status_map``.
DEFAULT_STATUS_MAP = {
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
    """One typed NetBox filter in a saved inventory scope."""

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
    value: str = Field(..., min_length=1, max_length=2048)

    @field_validator("field", "value")
    @classmethod
    def strip_scope_clause_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("scope clause field and value are required")
        return stripped


class NetBoxInventoryScope(BaseModel):
    """A named, reusable set of filters stored with one provider configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
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

    @field_validator("name")
    @classmethod
    def strip_scope_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("scope name is required")
        return stripped

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
            "connection test is otherwise a few hundred bytes. Nothing else reads it: a "
            "failure to fetch or parse it leaves the connection test's own result alone."
        ),
    )
    device_query: str = Field(
        "",
        max_length=2048,
        description=(
            "NetBox device filter applied to every device read, e.g. "
            "'status=active&role=wan-edge'. Auto-sync otherwise pulls the entire "
            "device table. A caller-supplied query is appended to this one."
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
    object_queries: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-family NetBox filter applied to every read of that family, the way "
            "device_query scopes devices, e.g. {'interface': 'mgmt_only=false', "
            "'ip_prefix': 'status=active'}. Keys are object type ids; a caller-supplied "
            "query is appended to the configured one."
        ),
    )
    inventory_scopes: list[NetBoxInventoryScope] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Named schema-aware filter sets. They are stored with the provider so they "
            "travel through Configuration Exchange; active_inventory_scope selects the "
            "one ANDead with legacy device_query/object_queries."
        ),
    )
    active_inventory_scope: str | None = Field(
        default=None,
        max_length=100,
        description="Name of the saved inventory scope applied to every provider read.",
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
        return value.strip()

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

    @field_validator("device_query", mode="before")
    @classmethod
    def normalize_device_query(cls, value: Any) -> str:
        """Accept a leading '?' and surrounding whitespace."""
        if value is None:
            return ""
        return str(value).strip().lstrip("?").strip()

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

    @field_validator("object_queries")
    @classmethod
    def validate_object_queries(cls, value: dict[str, str]) -> dict[str, str]:
        """Normalize per-family queries and reject keys naming no known family."""
        normalized: dict[str, str] = {}
        unknown: list[str] = []
        for object_type, query in value.items():
            if not isinstance(object_type, str):
                continue
            key = object_type.strip()
            if not key:
                continue
            try:
                validate_object_type_id(key)
            except ValueError:
                unknown.append(key)
                continue
            expression = str(query).strip().lstrip("?").strip() if query is not None else ""
            if expression:
                normalized[key] = expression
        if unknown:
            raise ValueError(
                f"object_queries has invalid object type id(s): {', '.join(sorted(unknown))}"
            )
        return normalized

    @model_validator(mode="after")
    def validate_inventory_scopes(self) -> NetBoxProviderConfig:
        names = [scope.name for scope in self.inventory_scopes]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"inventory scope names must be unique: {', '.join(duplicates)}")
        if self.active_inventory_scope is not None:
            active = self.active_inventory_scope.strip()
            if active not in names:
                raise ValueError("active_inventory_scope must name one of inventory_scopes")
            self.active_inventory_scope = active
        return self

    def active_scope(self) -> NetBoxInventoryScope | None:
        """The selected saved scope, or None when raw filters are the whole scope."""
        if self.active_inventory_scope is None:
            return None
        return next(
            (scope for scope in self.inventory_scopes if scope.name == self.active_inventory_scope),
            None,
        )

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
