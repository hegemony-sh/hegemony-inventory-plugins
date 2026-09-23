## [0.4.0](https://github.com/hegemony-sh/hegemony-inventory-plugins/compare/v0.3.0...v0.4.0) (2026-09-23)

### ⚠ BREAKING CHANGES

* **netbox:** a NetBox provider upgraded from an earlier release must be
discovered once before it syncs devices or sites.

The separate device and site reads are gone. `query_devices` and `list_sites`
were one of two reads of the same NetBox -- devices with their site nested
inside them keyed by slug, and the site listing keyed by numeric id -- and
keeping them as a fallback kept the disagreement that made one NetBox site into
two platform rows. They are abstract on the SDK base so they cannot be deleted;
they refuse instead, with one error naming the fix.

Failing is the point. Declining to serve devices at all would let a sync succeed
while refreshing nothing, and the first anyone would know is a run targeting a
device whose address changed weeks earlier. One loud failure that says "run
Discover" is the kinder one.

**Every provider setting still means what it meant.** That is most of this
change. The bindings were frozen constants, so removing the old path would have
silently stopped honouring six per-provider options -- including the `field_map`
paths that say which custom field holds an SSH credential reference. An operator
who had remapped one would have got devices whose credentials pointed at a field
nobody populates, with nothing to say so. Bindings are now composed from the
provider's own config in `discover()`: `status_map`, `tag_key_prefix`,
`default_access_config`, the four credential paths, and `site_tree_source`, which
picks which real family is bound beside sites rather than which nodes to invent.
`device_query` folds into the device family's scope, or a provider scoped to
three hundred devices would quietly widen to fifty thousand.

What cannot travel is reported rather than dropped. A `field_map` entry that
reaches into NetBox's raw payload in a way a stored attribute cannot express --
`vendor` is two levels down, a site is reached by an edge -- becomes a discovery
warning naming the key.

Also fixes a regression this branch introduced. A binding's `tags` is one path to
one key, and NetBox tags are a list, so projecting a device kept only `role` and
a curated NetBox tag silently stopped being selectable. `ProjectionBinding` gains
`tag_list` and `tag_prefix`, and `default_access_config` so a provider-wide
setting has somewhere to live.

One behaviour genuinely moves: a device's asset tag, rack, position, tenant and
location were copied onto the device row's `descriptive` and are attributes of
the `device` object now, where they are searchable like any other family's.

597 passed. The tests of the retired path are gone; the ones that used it only as
a vehicle -- auth headers, the retry ladder, off-origin pagination, error mapping
-- are rewritten against a family that still reads, because those guarantees did
not go anywhere.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016rQ9u3TYex2dXFfy2isnzL

### Features

* **netbox:** add schema-aware inventory scopes ([1ea534d](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/1ea534d41fbaa25c2aea3fb7373d3871fbc5974c))
* **netbox:** answer what changed since a marker, not only what is there ([71b954b](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/71b954b5f9f51f8dbc4dbc1fcaeee370ffc76b9d))
* **netbox:** bound how much a response may return, not only how long ([6472fe5](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/6472fe5cef12ba29f565ab432411ed3ca28f4d4e))
* **netbox:** derive the object-type catalogue from a NetBox OpenAPI schema ([c36b84d](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/c36b84d4fb35d7415455b04c6149f98cf4909ca1))
* **netbox:** generate the shipped catalogue instead of writing it by hand ([2b95d85](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/2b95d85777f4128ee4da040027a861535550d0ca))
* **netbox:** let the connection test ask an instance what it serves ([c21a7ce](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/c21a7cea85466c3946d59f8b491b255a49454a06))
* **netbox:** read any NetBox object family, and choose which in the config ([f97fa4a](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/f97fa4a1fdec9e46af3fe3a3b5d8e1cabf603b99))
* **netbox:** read devices and sites only as object families ([ed642bf](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/ed642bf5d3d7b72b352390397871656df649a53e))
* **netbox:** report the NetBox release and plugins from the connection test ([92bb606](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/92bb606b6e6d888be698463caac64e58f8b5ccf7))
* **netbox:** serve devices and sites as families, and bind them for projection ([1ce3cea](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/1ce3cea8f27d08df7274d8c04de01cdbe9a52f7b))
* **sdk:** carry device facts as generic attributes ([4ae051c](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/4ae051c1c4cabd0601d96503538717ac78b454ea))
* **sdk:** let a provider describe the instance it is configured against ([a2c5ee8](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/a2c5ee8da0232ee1bec9eb4db3bb5ef0c868b4f6))
* **sdk:** let a provider name the conversion a projected column needs ([d0b365b](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/d0b365beea53fab64609c4d697d6a1465c137627))
* **sdk:** let an object type name the family it belongs to ([e72933c](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/e72933c2002b2f991e85d20696cd3fe3f8165374))
* **sdk:** let the platform hand a provider the family it discovered ([d21defa](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/d21defac7fd5eb4abae6b9aa5ad733aaa73eb9e6))

### Bug Fixes

* **ci:** skip attestation while the repository is private ([#9](https://github.com/hegemony-sh/hegemony-inventory-plugins/issues/9)) ([30e751d](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/30e751d56aa9684cee9986508a2a2963c2e2c4bb))
* **git:** keep rejected input out of validation errors ([9cf7ebf](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/9cf7ebfce43f7b0d9933bfa7172817b508fc131b))
* **netbox:** conjoin scope filters by NetBox lookup family ([4d1e451](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/4d1e45156451bb73582e0028a8e85ce0490e62e6))
* **netbox:** drop the config's own object-type setting, leaving one control ([2af47e7](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/2af47e7cd694db370d5c9b7bd013b999205e4b59))
* **netbox:** enforce scoped and complete inventory reads ([ebeb6e2](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/ebeb6e2f6f8d03b21d52b2590abb64726ac84ac1))
* **netbox:** harden NetBox projection and sync ([0f2e9c6](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/0f2e9c6ccfd0c1f4c208f1f2d66b64f891e904fc))
* **netbox:** key sites consistently, finish the IPAM contract, and read more of NetBox ([#10](https://github.com/hegemony-sh/hegemony-inventory-plugins/issues/10)) ([d6e003e](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/d6e003ea6e08f8c04c498a6d6e085a4a80bfd59f))
* **netbox:** let a pre-4.2 circuit termination reach its provider network ([383c91e](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/383c91eef1a31266a13fefc7f3dff35bc4003815))
* **netbox:** make derived edges resolve, and keep both spellings searchable ([1f123ab](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/1f123ab9ebf92e54ad7360cd5c0587c309847d42))
* **netbox:** map the relations NetBox made generic, and scope single reads ([e3254af](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/e3254afb93ef3868f17c2ee359e0c32b36c55ffc))
* **netbox:** offer every family, so the platform's selection is the choice ([cc984d3](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/cc984d3206abd092267a75ab11b144dbfe9d82dc))
* **netbox:** overlay remapped credential refs onto the defaults ([3836e67](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/3836e67f1042e788f416fb9ece6033c052c4adde))
* **netbox:** page NetBox listings ordered by id ([5a1e52b](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/5a1e52b96d27c551e51e8b9b2106e74dc22b2866))
* **netbox:** preserve complete projected inventory ([0d0bb60](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/0d0bb60864ce3d519f34001054969db72f02eeb5))
* **netbox:** read every custom field, and let discovery complete a shipped mapping ([3ad0a49](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/3ad0a4965c4922022c3a414330076d6215c45848))
* **netbox:** reject malformed legacy scope containers ([7fa2199](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/7fa2199e9b7a22fbc2f779aa321ae4ed1e22beaa))
* **netbox:** request JSON when validating shared schema access ([8584192](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/858419285b784446e92227af93d27d658f1b3abb))
* **netbox:** say that discovery has not run when a rule cannot be validated ([c9a3399](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/c9a33993d53fd3002bb5ff13d8146fa23bbdd1fe))
* **netbox:** start delta reads a grace window behind the marker ([f687e05](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/f687e05cce9900230505ec10b66ed8b05aa3d511))
* **netbox:** stop naming spellings a current NetBox does not serve ([c7a50e6](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/c7a50e625d76e78f3452cda4309c1ebf4231ca58))
* **netbox:** unify inventory filters and share schema discovery ([27c313c](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/27c313c91c7fbbdf3442cf9fedb817d8ccef0602))
* **netbox:** use NetBox's own content-type label for VM interfaces ([a385f02](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/a385f02c140aa1fa3dca671f5882edfc30089ffe))
* **netbox:** validate projections and normalize scope errors ([9b5f13f](https://github.com/hegemony-sh/hegemony-inventory-plugins/commit/9b5f13f7b4d1fdf3a76614ba42495ec66dc4bcd2))

# Changelog

Generated by semantic-release on every release; do not edit by hand.
