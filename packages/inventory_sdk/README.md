<!--
SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# hegemony-inventory-sdk

Public, dependency-light SDK for building **Hegemony inventory plugins** —
out-of-tree provider and object-type wheels that the core platform loads at
runtime.

A plugin depends only on this package (which depends only on `pydantic`) and
exposes a `register(registry)` callable under the `hegemony.inventory_plugins`
entry-point group:

```toml
# In your plugin's pyproject.toml. The SDK is 0.x, where a breaking change
# releases a new minor version, so depend on one minor at a time.
[project]
dependencies = ["hegemony-inventory-sdk>=0.4.1,<0.5"]

[project.entry-points."hegemony.inventory_plugins"]
my_plugin = "my_plugin:register"
```

```python
# my_plugin/__init__.py
from hegemony_inventory_sdk import InventoryPluginRegistry, InventoryProvider


def register(registry: InventoryPluginRegistry) -> None:
    registry.register_provider_type(
        provider_type="acme",
        display_name="Acme",
        description="Acme inventory",
        capabilities=["read", "query"],
        supported_resources=["device"],
        factory=build_acme_provider,
        config_model=AcmeConfig,
    )
```

The public contract — provider base class, value objects, enums, error
envelopes, the object-type declaration API, and the registry/secret/transport
protocols — is re-exported from the top-level `hegemony_inventory_sdk` package.
`SDK_ABI_VERSION` identifies the registration ABI; the core platform pins a
compatible range.

## Describing an instance, not just a provider type

The object types declared in `register()` belong to the provider *type*, and are the
same for every instance of it. That is right for a source of truth with a fixed shape
and wrong for one that can be extended: a NetBox with plugins installed serves models
the plugin was never written to know about, every instance has its own custom fields,
and an older release does not serve everything a newer one does.

A provider that can answer for its own instance declares `Capability.DISCOVER` and
implements `discover()`:

```python
async def discover(self, *, context: ProviderCallContext) -> DiscoveryResult:
    return DiscoveryResult(
        source_version="4.3.0",
        object_types=(spec,),                  # what the platform renders and stores
        mappings={spec.id: mapping},           # how this provider reads each one
        warnings=(),                           # shapes it could not interpret
    )
```

`object_types` and `mappings` are two halves of one answer and are keyed alike — a spec
without its mapping is an object type nothing can fetch, and `DiscoveryResult` rejects
the mismatch where it is built rather than where it is used.

Two rules are worth stating because they are easy to get wrong:

- **Extend, do not shadow.** A family the plugin already registers should keep the id it
  registers, or an instance ends up offering two object types for the same family and
  every row already synced under the first is stranded. Only families the plugin does
  not know should arrive under a new id.
- **Discovery is expensive by nature.** It describes a whole API rather than reading one
  object, so the platform calls it on demand and stores the answer rather than running
  it per sync. `source_version` is what tells the platform a stored answer has gone
  stale.

The default implementation raises `not_supported`, so a provider whose shape really is
fixed needs no change at all.

## API reference

The SDK exposes Python interfaces, not an HTTP server. Import public types from
`hegemony_inventory_sdk`. The tables below describe the main inputs and outputs;
Python's standard-library `pydoc` renders the complete signatures, defaults,
dataclass fields and docstrings for the installed SDK without reading source files.

From the repository root:

```bash
uv sync --frozen --all-packages
uv run python -m pydoc hegemony_inventory_sdk
uv run python -m pydoc hegemony_inventory_sdk.provider.InventoryProvider
uv run python -m pydoc hegemony_inventory_sdk.descriptors
uv run python -m pydoc hegemony_inventory_sdk.discovery
uv run python -m pydoc hegemony_inventory_sdk.mapping
uv run python -m pydoc hegemony_inventory_sdk.objecttype
uv run python -m pydoc hegemony_inventory_sdk.registry
uv run python -m pydoc hegemony_inventory_sdk.services
uv run python -m pydoc hegemony_inventory_sdk.errors
```

For a browsable local HTML reference, run `uv run python -m pydoc -b`,
open `hegemony_inventory_sdk`, and follow its module and class links.
Stop the server when finished. In an environment with the released SDK already
installed, omit `uv run` and use that environment's Python interpreter.

### Registration and provider methods

A plugin's synchronous `register(registry)` entry point receives an
`InventoryPluginRegistry`. Its `register_provider_type(...)` method accepts
the provider identity and description, capabilities, supported resources,
factory, and optional configuration model/schema/defaults; it returns `None`.
`register_object_type(spec)` accepts an `ObjectTypeSpec` and returns `None`.

Each asynchronous provider call takes keyword-only `context: ProviderCallContext`,
which identifies the provider and operation and may carry run metadata, a
discovered object mapping, a query schema, and a `since` marker.
Query syntax and supported filters are provider-specific; see the
[NetBox](../../plugins/inventory_netbox/README.md) and
[Git](../../plugins/inventory_git/README.md) configuration references.

| Method | Inputs besides context | Output |
| --- | --- | --- |
| `list` | `resource_type`, optional query mapping, keyword-only `limit` and `cursor` | List of `ResourceRef` values |
| `get` | `resource_type`, `external_id` | `DeviceDescriptor`, `SiteRef`, or `ResourceRef` |
| `query_devices` | Provider-specific `expr`, optional keyword-only `limit` | List of `DeviceDescriptor` values |
| `list_sites` | Optional keyword-only `limit` | List of `SiteRef` values |
| `test_connection` | No additional inputs | `ProviderTestResult` |
| `list_objects` | `object_type`, optional query mapping, keyword-only `limit` and `cursor` | List of `ObjectDescriptor` values |
| `get_object` | `object_type`, `external_id` | One `ObjectDescriptor` |
| `iter_object_pages` | `object_type`, optional query mapping | Async iterator yielding lists of `ObjectDescriptor` values |
| `discover` | No additional inputs | `DiscoveryResult` |
| `update` | `resource_type`, `external_id`, patch mapping | `None`; unsupported by the base implementation |

Implement the five abstract device/site methods. The generic list/get defaults
adapt device/site results and reject other object types. For sites, the default
`list_objects` adapter calls `list_sites` and ignores `query`; providers that
support filtered site reads must override `list_objects`. The default page
iterator yields one page from `list_objects`. Discovery and updates are optional;
the base implementations raise an `InventoryProviderError`.

Two methods are synchronous and do not take a call context:
`configured_projection_bindings(object_types)` returns a tuple of
`ProjectionBinding` values or `None` (keep stored bindings), and
`parse_webhook(headers, body)` accepts a header mapping and bytes and returns
a list of `ChangeEvent` values. The base webhook implementation is unsupported.

### Returned values and declarations

| Type | Contract |
| --- | --- |
| `ResourceRef` | Provider id, resource type, external id, display name and descriptive metadata |
| `DeviceDescriptor` | Provider id, external id, name and management host, with optional platform, site, attributes, tags, access references and source version |
| `SiteRef` | Provider id, external id and name, with optional parent, location, tags and descriptive metadata |
| `ObjectDescriptor` | Provider id, object type, external id and name, plus attributes, relationships, access references and source version |
| `ObjectRelationship` | Relationship name, target object type and target external id |
| `ProviderTestResult` | Success flag, provider identity, optional latency, message, error and safe details |
| `ObjectTypeSpec` | Type id and display name, output field schema, separate input query schema, identity keys, relationships and UI hints; `from_model(...)` derives the field schema from a Pydantic model |
| `DiscoveryResult` | Source version, object type specifications, mappings, projection bindings and warnings; specification ids and mapping keys must match |
| `ObjectFieldMapping` | Paths mapping a source record into generic identity, fields and relationships |
| `ProjectionBinding` | Declares how an object family maps onto the platform's device or site representation |
| `ChangeEvent` | Provider/resource identity, change type, optional timestamp, payload and source event id |

For delta-capable providers, return an ordering marker in
`source_version["marker"]` and include records at or after `context.since`.
Markers must sort lexicographically in source order. `context.since is None`
requests a full read. An individual descriptor without a marker does not force
a full read or clear the saved marker; the host retains the highest available
marker. Records without markers may remain stale until scheduled full
reconciliation, which also detects deletions.

### Host services and errors

The host injects `PlatformServices` at construction. It supplies scale limits
and the following methods:

| Method | Calling convention | Result |
| --- | --- | --- |
| `resolve_secret_ref(ref, *, operation)` | Async; must be awaited | A secret string or `None` |
| `validate_url(url, *, operation)` | Async; must be awaited before making the request | The validated host |
| `validate_access_config(...)` | Synchronous | A validated dictionary |
| `git.get_workdir(...)` | Async; must be awaited | A `GitWorkdir` containing a checkout path and pinned commit |

For example, use `await services.validate_url(url, operation=context.operation)`
before accessing a provider URL. Calling it without `await` does not perform
validation. Secret storage, URL policy and Git authentication belong to the host.

Provider failures use `InventoryProviderError.envelope`, a
`ProviderErrorEnvelope` with provider identity, operation, error code, safe
message, retryability, optional resource type and safe details.
`envelope.to_dict()` produces a serializable response dictionary.
Do not place resolved credentials or other secrets in descriptors, messages,
cause chains or safe details.
