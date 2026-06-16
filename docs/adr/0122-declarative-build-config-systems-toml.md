# ADR 0122 — Declarative `[[build_config]]` home in `systems.toml` (#443)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):**
  [ADR-0096](0096-kdump-config-fragment-build-input.md) (the build-config catalog, reserved
  object key, the read tool and build-path fetch this leaves unchanged),
  [ADR-0119](0119-operator-build-config-write-path.md) (the `source` provenance column, the
  shared `BUILD_CONFIG` per-name lock, the row-vs-object non-atomicity contract, the
  `buildconfig.set` write-path this composes with),
  [ADR-0112](0112-systems-inventory-config.md) (the `systems.toml` inventory + reconcile
  engine and its `managed_by` ownership layers),
  [ADR-0115](0115-declarative-cost-class-coefficients.md) (the closest reconcile precedent:
  a declarative, file-authoritative `[[cost_class]]` block with a shared neutral validation
  rule and loud drift).
- **Issue:** [#443](https://github.com/randomparity/kdive/issues/443).
- **Spec:** [`../design/declarative-build-config-systems-toml.md`](../design/declarative-build-config-systems-toml.md).

## Context

ADR-0119 (#438) gave build-config fragments an imperative operator write-path
(`buildconfig.set`, `platform_admin`-gated). A fragment now reaches `build_config_catalog`
two ways: the packaged deploy-time seed (`source='seed'`) and `buildconfig.set`
(`source='operator'`). There is **no declarative `systems.toml` home**.

`systems.toml` (v2, ADR-0112) already reconciles `[[image]]`, the provider-instance blocks,
`[[build_host]]`, and `[[cost_class]]` (ADR-0115), but the inventory reconcile engine
(`src/kdive/inventory/`) does not touch `build_config_catalog`. So `[[cost_class]]` is
declarative pricing policy in the operator's GitOps-managed file, but kernel-config policy
(the `kdump` fragment and friends) is not — an operator who manages their fleet through
`systems.toml` + GitOps cannot declare a build-config fragment there.

The tension this ADR resolves: the existing `systems.toml` reconcile is **file-authoritative**
(config wins; `[[cost_class]]` re-asserts the file value, image/resource/build_host passes
prune departed rows), while ADR-0119 deliberately built the seed to **never clobber an
operator override** (`source` column + `WHERE source='seed'` guard + the shared
`BUILD_CONFIG` lock). Build-config becomes the first object with **both** a declarative
config home and an imperative override that must survive `migrate`. The five decisions below
settle how those coexist.

## Decision

### 1. Source precedence — a third `source='config'` that beats `operator`, loudly

A name **declared in the file** is file-owned: reconcile re-asserts the file bytes on every
pass and writes the row with `source='config'`. A name **not** in the file is never touched
by reconcile — `seed`/`operator` keep today's #438 behavior exactly.

This is the cost_class §2 model (ADR-0115) verbatim: a class named in the file is file-owned
and a runtime override on it is transient; a class not in the file is `ops`-owned and
untouched. For build-config, a `buildconfig.set` on a **declared** name still writes
`source='operator'`, but the next reconcile re-asserts the file value and flips the row back
to `source='config'`. The clobber's loudness is **path-dependent**: the continuous
reconciler loop (which does most of the clobbering) emits a `ReconcileDiff.warned` entry and
a drift log line but **no** audit row; only the on-demand `ops.reconcile_systems` path writes
a `platform_audit_log` row. So a `buildconfig.set` on a declared name is transient (reverted
within roughly one reconcile interval, the cost_class §6 window) and is for break-glass only
— the durable change is editing the file. Names not in the file keep `buildconfig.set`'s full
durability.

The #438 **seed-vs-operator** protection is untouched. The seed's `WHERE source='seed'`
conflict guard already refuses to overwrite any non-`seed` row, so it refuses both
`operator` and `config`. Seed-vs-operator (the packaged default must not win over a
deliberate override) and config-vs-operator (a deliberately-declared file value wins over a
live override) are different questions with different answers; this design keeps both.

### 2. Prune semantics — upsert-only, never prune

Removing a `[[build_config]]` block is a **no-op**: the row persists at its last-asserted
bytes, `source` stays `config`, the object stays at the reserved key. No prune, no cordon.

This is the cost_class §3 model, chosen over the prune-if-idle/cordon-if-live contract the
image/resource/build_host passes use, because a build-config fragment is consumed
**transiently** at build time (a build job fetches it by name) — there is no persistent
"live reference" row a reconcile could safely check the way an allocation references a
resource, so the cordon-if-live concept does not apply, and a reconcile-driven delete could
strand an in-flight build's fetch. Upsert-only is the safest non-destructive default.

### 3. Content representation — inline `content` string

The fragment text lives inline in the block (`content = """…"""`). The reconcile pass reads
it, computes sha256, and PUTs to the **same reserved object key** the seed and
`buildconfig.set` use (deterministic in `name`; overwrites in place, no orphan).

Chosen over a path reference and a pre-published S3 ref: AC#1 requires reconcile to
*publish* the bytes (ruling out a `kind:s3` ref the operator uploads separately), inline
keeps one version-controlled file and one k8s ConfigMap (GitOps-native, the
`buildconfig.set` content model), and fragments are a few KiB (cap
`KDIVE_MAX_BUILD_CONFIG_BYTES`, default 256 KiB) so ConfigMap bloat is negligible. A path
reference would keep the file lean but force the fragment files to ship alongside
`systems.toml` (a second mount) for a marginal saving on KiB content.

### 4. Reconcile pass ordering and lock

A new `inventory/reconcile_build_configs.py` pass is appended **last** in `reconcile_all`
(`images → coefficients → resources → build_hosts → build_configs`). It has no cross-entity
dependency, so position is free; last keeps the merged diff stable. Each fragment publishes
under `advisory_xact_lock(LockScope.BUILD_CONFIG, name)` — the **same** per-name lock the
seed and `buildconfig.set` take — mandatory by the ADR-0119 row-vs-object argument (the
object PUT and the row upsert are separate writes; reconcile, seed, and `set` must serialize
per name or one commits a row sha256 that describes another writer's bytes). `BUILD_CONFIG`
is held alone, outside the cross-scope co-hold total order, so a third taker changes no lock
ordering.

The pass consumes the **same `store` object** the reconcile already threads, narrowed via a
`runtime_checkable` publish-capable protocol (`head_present` + `put_artifact`). The
reconciler loop and the on-demand path (with S3 configured) pass a concrete `ObjectStore`,
which can publish; the on-demand `_AbsentImageStore` (no S3) cannot, so the pass appends a
`warned` record and leaves declared rows untouched — the store-down degrade the image pass
already uses. This keeps `reconcile_all`'s call signature unchanged, so the only M2-gated
file the change touches is the new migration.

### 5. Interaction with the deploy seed — layer, no seed-logic change

ADR-0121 already moved the build-config seed **out of `migrate()`** (now SQL-only) into the
`seed-build-configs` command, wired as a `post-install`/`post-upgrade` Helm hook (weight 10,
after the weight-0 `migrate` and the weight −10 `systems.toml` validate). The reconciler loop
runs continuously, not as a deploy hook. Seed and config **layer**: `seed-build-configs`
publishes the packaged default for any name **not** declared in config; for a declared name,
the continuous reconciler loop flips the row to `source='config'` and the seed's existing
`WHERE source='seed'` guard then refuses to touch it (it already refuses any non-`seed` row).
The only seed change is the skip condition in `seed.py` (skip when the stored `source` is
`operator` **or** `config`, was `operator` only) and the migration's CHECK gaining
`'config'`. The seed's source-guarded upsert SQL is unchanged. On a fresh install where the
operator declares a fragment with bytes differing from the packaged default, a build in the
brief window before the loop's first pass fetches the packaged bytes (acceptable eventual
consistency; the seed bytes are a valid fragment).

### Schema and shared code

- Migration `0035` drops and re-adds the `source` CHECK to allow `'config'` (no new column).
  Provider-agnostic core, so it joins the M2 portability-gate `ALLOWED_FILES`
  (`scripts/m2_portability_gate.py`) and its meta-test frozenset, as `0034` did.
- Validation splits by whether it needs config. The config-free checks (`name` charset,
  non-empty `content`) move into a neutral `build_configs/rules.py` (mirroring
  `domain/cost_class_rules.py`) and run at parse time in the model (the loader stays pure —
  `model.py` does not import `kdive.config`); the helper raises a bare `ValueError` each
  caller maps to its own error type. The config-dependent **byte cap** is **not** enforced in
  the pydantic model (no DI seam, would break loader purity and the no-DB `--check` mode);
  the reconcile pass enforces it just before publishing (it already reads config), as a
  per-fragment `warned` skip, reading the **same** `MAX_BUILD_CONFIG_BYTES` as
  `buildconfig.set` so the two cannot diverge. `inventory/` must not import `mcp/`;
  `build_configs/` is neutral and importable by both.
- `build_configs/catalog.py` gains `upsert_config_build_config` (writes `source='config'`
  unconditionally) and a `(sha256, source)` reader the pass uses for change-detection and
  drift attribution.

## Consequences

- The kernel-config baseline becomes a reviewable, reproducible artifact in the inventory
  file, closing the dev/operator-separation asymmetry (#428) for build-config the way
  ADR-0115 closed it for pricing.
- One additive migration (`0035`), one new inventory pass, one new neutral rule module, two
  new catalog writers/readers, one new `InventoryDoc` field, one widened seed skip
  condition. No new MCP tool, no new column, no new env knob, no new lock scope. The
  `buildconfig.get`/`set` tools, the build-path fetch, the reserved-key scheme, and the flat
  output schema (ADR-0113) are unchanged.
- A `buildconfig.set` on a **declared** fragment is transient (re-asserted to the file value
  on the next reconcile, with a `warned` entry). The durable way to change a declared
  fragment is to edit the file. This is the deliberate cost of a file-authoritative
  declarative home, identical to the cost_class trade.
- Removing a `[[build_config]]` block does not delete the row (upsert-only). An operator who
  wants the fragment gone reverts it explicitly; a `buildconfig.delete`/`reset-to-seed` is a
  possible follow-up, out of scope here (no speculative surface).

## Considered & rejected

- **Operator wins (a live `buildconfig.set` override survives reconcile).** Rejected: it
  protects the #438 invariant literally, but makes build-config the one `systems.toml` pass
  where the file is **not** authoritative — inconsistent with `[[image]]`, `[[cost_class]]`,
  `[[build_host]]`, and resources. The #438 invariant is specifically about the *seed*
  (the packaged default) not clobbering an override, which this design keeps; it is not a
  promise that a *declared* file value yields to a live override.
- **Reject-and-warn on a config/operator name collision.** Rejected: it is the safest against
  a silent clobber, but the clobber is **not** silent here (Decision 1: `warned` + log +
  audit), and reject-and-warn leaves the file declaring something that silently does not
  apply — a worse failure mode for a GitOps source of truth. The cost_class clobber+warn
  model is the established precedent.
- **Prune-if-idle / cordon-if-live on removal (the image/resource/build_host contract).**
  Rejected: a build-config fragment has no persistent live-reference row to gate a cordon on
  (it is fetched transiently at build time), so the liveness predicate the resource/image
  prune relies on does not exist for it. Upsert-only (cost_class §3) is the correct
  non-destructive analogue.
- **Path reference (`path = "…"` relative to the file).** Rejected: keeps the file lean but
  forces the fragment files to ship alongside `systems.toml` (a second k8s mount; the file
  and its fragments must travel together) for a marginal size saving on KiB content. Inline
  is simpler and matches the `buildconfig.set` content model.
- **Pre-published S3 ref (`kind:s3`, object_key + digest, HEAD-only like `[[image]]`).**
  Rejected: AC#1 requires reconcile to *publish* the bytes to the reserved key; an S3 ref
  pushes the operator out of the declarative file (a separate upload), defeating "declare a
  fragment in `systems.toml`."
- **A new `build_config_store` parameter on `reconcile_all` and its two callers.** Rejected
  in favor of narrowing the existing threaded `store` via a runtime-checkable protocol: a new
  parameter would edit `reconciler/inventory.py` and `mcp/tools/ops/reconcile_systems.py`
  (both M2-gated core), enlarging the portability-gate allowlist for no behavioral gain. The
  capability narrow keeps the only gated touch the migration.
- **A `[[build_config]]` export tool (the `ops.export_cost_classes` analogue).** Out of scope:
  not requested by #443, and the cost_class export exists to capture an *ops-owned* override
  back into the file — build-config's durable change is editing the file directly. It would
  be the build-config sibling of #429 if wanted later.
