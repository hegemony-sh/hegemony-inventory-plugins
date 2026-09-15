<!--
SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>

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
# In your plugin's pyproject.toml
[project.entry-points."hegemony.inventory_plugins"]
my_plugin = "my_plugin:register"

[project.dependencies]
hegemony-inventory-sdk = ">=0.1,<0.2"
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
