# Declarative `[[build_config]]` home in `systems.toml` (#443)

- **Issue:** [#443](https://github.com/randomparity/kdive/issues/443)
- **ADR:** [ADR-0122](../adr/0122-declarative-build-config-systems-toml.md)
- **Status:** Draft
- **Date:** 2026-06-15

## Problem

[#438](https://github.com/randomparity/kdive/issues/438) (ADR-0119) gave build-config
fragments an **imperative** operator write-path (`buildconfig.set`, `platform_admin`-gated).
A fragment now reaches `build_config_catalog` two ways: the packaged deploy-time seed
(`source='seed'`) and `buildconfig.set` (`source='operator'`). There is **no declarative
`systems.toml` home**.

Today `systems.toml` (v2, ADR-0112) reconciles `[[image]]`, the provider-instance blocks,
`[[build_host]]`, and `[[cost_class]]` (ADR-0115), but the inventory reconcile engine does
not touch `build_config_catalog`. This is an asymmetry: `[[cost_class]]` is declarative
policy in the operator's GitOps-managed file, but kernel-config policy (the `kdump` fragment
and friends) is not. An operator who manages their fleet through `systems.toml` + GitOps
cannot declare a build-config fragment there.

## Goals

- An operator declares a build-config fragment in `systems.toml` and `reconcile-systems`
  publishes it to `build_config_catalog` + the reserved object key.
- The config-vs-operator precedence is explicit, documented, and tested.
- Removing a declared fragment has a defined, non-destructive outcome.

## Non-goals

- A `[[build_config]]` **export** tool (the sibling of [#429](https://github.com/randomparity/kdive/issues/429); not requested here).
- A `buildconfig.delete` / `reset-to-seed` tool (out of scope, as in ADR-0119).
- Path-reference or pre-published S3-ref content forms (the AC requires reconcile to
  *publish* the bytes; see Decision 3).

## The five settled decisions

### 1. Source precedence — `config` beats `operator`, loudly

A third provenance value `config` joins `seed`/`operator`. A fragment **name declared in
the file** is file-owned: reconcile re-asserts the file bytes on every pass and writes the
row with `source='config'`. A name **not** in the file is never touched by reconcile —
`seed`/`operator` keep today's #438 behavior exactly.

This is the cost_class §2 model (ADR-0115) applied verbatim: a class named in the file is
file-owned and a runtime override on it is transient; a class not in the file is
`ops`-owned and untouched. For build-config, a `buildconfig.set` on a **declared** name
still writes `source='operator'`, but the next reconcile re-asserts the file value and
flips it back to `source='config'`.

**The clobber's loudness is path-dependent — do not overstate it.** The continuous
reconciler loop (`reconciler/inventory.py`), which does *most* of the clobbering because it
runs every interval, emits only a `ReconcileDiff.warned` entry and a drift log line — it
writes **no** `platform_audit_log` row. The `platform_audit_log` row exists **only** on the
on-demand `ops.reconcile_systems` path. So an operator's `buildconfig.set` on a declared
name is typically reverted within one reconcile interval, with only a log line as the
operator-visible trace (the `warned` entry is surfaced in the on-demand response and the
loop's change count, not pushed anywhere). The durable way to change a declared fragment is
to **edit the file**; `buildconfig.set` on a declared name is for break-glass only and is
expected to be transient (this is the same window cost_class §6 documents). Names **not** in
the file keep `buildconfig.set`'s full durability — they are never reconciled.

The #438 **seed-vs-operator** protection is untouched. The seed's `WHERE source='seed'`
conflict guard already refuses to overwrite any non-`seed` row, so it refuses both
`operator` and `config` rows. Seed-vs-operator (the packaged default must not win) and
config-vs-operator (a deliberately-declared file value wins) are different questions with
different answers, and this design preserves both.

**Why not "operator wins" or "reject-and-warn":** either would make build-config the one
`systems.toml` pass where the file is **not** authoritative, diverging from `[[image]]`,
`[[cost_class]]`, `[[build_host]]`, and resources. "Reject-and-warn" additionally leaves
the file declaring something that silently does not apply. The GitOps contract is that the
version-controlled file is the source of truth; a declared value taking effect (and saying
so when it overrode a live override) is the least-surprising behavior.

### 2. Prune semantics — upsert-only, never prune

Removing a `[[build_config]]` block from the file is a **no-op**: the catalog row persists
at its last-asserted bytes, `source` stays `config`, and the object stays at the reserved
key. No prune, no cordon.

This is the cost_class §3 model (ADR-0115), chosen over the prune-if-idle/cordon-if-live
contract the image/resource/build_host passes use. A build-config fragment is consumed
**transiently** at build time (a build job fetches it by name); there is no persistent
"live reference" row a reconcile could safely check the way an allocation references a
resource. A reconcile-driven delete could strand an in-flight build's fetch. Upsert-only is
the safest non-destructive default. Reverting a fragment to the packaged default or
deleting it is an explicit operator act (`buildconfig.set` the packaged bytes), not a
silent consequence of a file edit. A `buildconfig.delete` is a possible follow-up.

### 3. Content representation — inline `content` string

```toml
[[build_config]]
name = "kdump"
description = "kdump/debuginfo kernel-config fragment"
content = """
CONFIG_KEXEC=y
CONFIG_CRASH_DUMP=y
CONFIG_DEBUG_INFO=y
"""
```

The reconcile pass reads `content`, computes its sha256, and PUTs the bytes to the **same
reserved object key the seed and `buildconfig.set` use** (`tenant=system`,
`owner_kind=build-configs`, `owner_id=<name>`, `name=<name>.config`,
`Sensitivity.REDACTED`, `retention_class=build-config`). The key is deterministic in
`name`, so a re-asserted fragment overwrites in place — no orphaned object.

Inline is chosen over a path reference (relative to the file) and over a pre-published S3
ref:

- **AC#1 requires reconcile to *publish* the bytes** to the reserved key, which rules out a
  pre-uploaded `kind:s3` ref (where the operator uploads separately and reconcile only
  HEADs).
- **Inline keeps one version-controlled file** — GitOps-native, the same content model
  `buildconfig.set` uses, and a single k8s ConfigMap. Fragments are a few KiB (cap
  `KDIVE_MAX_BUILD_CONFIG_BYTES`, default 256 KiB), so ConfigMap bloat is negligible.
- **A path reference** would keep the file lean but require the referenced fragment files to
  ship alongside `systems.toml` (a second mount/ConfigMap in k8s; the file and its fragments
  must travel together) — more moving parts for a marginal size saving on KiB content.

### 4. Reconcile pass ordering and lock

A new pass `inventory/reconcile_build_configs.py` is appended **last** in `reconcile_all`:
`images → coefficients → resources → build_hosts → build_configs`. The build-config pass
has no cross-entity dependency (reconcile does not validate the fragment↔System linkage),
so its position is free; last keeps the merged diff stable.

Each fragment is published under `advisory_xact_lock(LockScope.BUILD_CONFIG, name)` — the
**same** per-name lock the seed and `buildconfig.set` take. This is mandatory by the
ADR-0119 row-vs-object argument: the object PUT and the row upsert are separate writes, so
reconcile, seed, and `set` must serialize per name or one can commit a row sha256 that
describes another writer's bytes (which then trips `verify_bytes` on every read until a
re-write). `BUILD_CONFIG` is always held alone (outside the cross-scope co-hold total
order), so adding a third taker does not affect lock ordering.

### 5. Interaction with the deploy seed — layer, no seed-logic change

ADR-0121 already moved the build-config seed **out of `migrate()`**: `migrate()` is now
SQL-only, and the seed runs as the `seed-build-configs` command, wired as the
`post-install`/`post-upgrade` Helm hook (`job-seed-build-configs.yaml`, weight 10). So the
deploy-time order is: validate `systems.toml` (`pre-install` hook, weight −10) → `migrate`
(SQL, weight 0) → `seed-build-configs` (post, weight 10). The reconciler loop runs
**continuously** in the running deployment (not a deploy hook).

Seed and config **layer**. The `seed-build-configs` step publishes the packaged default for
any name **not** declared in config. For a **declared** name, the continuous reconciler loop
flips the row to `source='config'`, and the seed's existing `WHERE source='seed'` conflict
guard then refuses to touch it on any later `seed-build-configs` run (it already refuses any
non-`seed` row). The only seed change is the skip condition in `seed.py` (skip when the
stored `source` is `operator` **or** `config`, was `operator` only) and the migration's CHECK
constraint gaining `'config'`. The seed's source-guarded upsert SQL is unchanged.

**Fresh-install ordering window.** On a first install where the operator declares
`[[build_config]] name="kdump"` with bytes that differ from the packaged default,
`seed-build-configs` publishes the packaged bytes (`source='seed'`) before the running
reconciler loop's first pass re-asserts the file bytes (`source='config'`). A build started
in that sub-interval window fetches the packaged bytes, not the declared ones. This is
acceptable eventual consistency (the next reconcile converges and the loop runs on a short
interval); it is called out, not engineered around, because the seed bytes are a valid
kdump fragment and no build correctness invariant depends on the declared override landing
before the first reconcile.

## Architecture

### Data model

Migration `0035` widens the provenance CHECK:

```sql
ALTER TABLE build_config_catalog DROP CONSTRAINT build_config_catalog_source_check;
ALTER TABLE build_config_catalog
    ADD CONSTRAINT build_config_catalog_source_check
        CHECK (source IN ('seed', 'operator', 'config'));
```

No new column. The migration is provider-agnostic core (`db/schema/`), so it is added to
the M2 portability-gate `ALLOWED_FILES` (`scripts/m2_portability_gate.py`) and its meta-test
frozenset (`tests/scripts/test_m2_portability_gate.py`), the same as migration `0034`
(ADR-0119).

### Shared validation rule, and where each check runs

`buildconfig.set` validates two things at its write boundary: `name`
(`^[a-z0-9][a-z0-9_-]{0,63}$`) and `content` (non-empty UTF-8 ≤
`KDIVE_MAX_BUILD_CONFIG_BYTES`). These split cleanly by whether they need config:

- **Config-free checks (`name` charset, content non-empty)** move into a neutral
  `build_configs/rules.py` — mirroring `domain/cost_class_rules.py` — and run **at parse
  time** in the inventory model's field validators. They need no runtime value, so the
  loader stays pure (it imports no config singleton; `model.py` does not import
  `kdive.config` today and must not start). The helper raises a bare `ValueError`; each
  caller maps it (`InventoryError` at file load, `CONFIGURATION_ERROR` for the tool).
- **The byte cap (≤ `KDIVE_MAX_BUILD_CONFIG_BYTES`)** is config-dependent and is **not**
  enforced in the pure model/loader — a pydantic field validator has no clean seam to inject
  the runtime cap, and reading config inside `model.py`/`load_inventory` would break the
  loader's purity. It is instead enforced at the **two layers that already read config**, both
  off the same `config.require(MAX_BUILD_CONFIG_BYTES)` (so they cannot diverge):
  - **Deploy-time, in `reconcile-systems --check` (`validate_systems`).** This is the
    `pre-install`/`pre-upgrade` fail-fast Helm gate (ADR-0121). It already reads
    `kdive.config` (it resolves `SYSTEMS_TOML`), and an env read is within its no-DB/no-S3
    contract, so after parsing it checks each declared fragment's UTF-8 byte length against
    the cap and exits non-zero (the same fail-the-deploy behavior a bad `name` gets). This
    keeps the deploy-time safety net closed: an over-cap fragment aborts the upgrade rather
    than deploying green and silently not publishing.
  - **Runtime, in the reconcile pass.** As the authority on a running system: an over-cap
    declared fragment is a per-fragment `warned` skip (row untouched), not a whole-pass
    failure — one oversized fragment must not block the others, matching the image pass's
    per-entry degrade. This still fires for a fragment that grew past the cap *after* a deploy
    (e.g. a live ConfigMap edit the reconciler picks up without a `--check`).

  The cap is therefore caught before deploy **and** defended at runtime; the pure model is
  the only layer that does not check it.

`inventory/` must not import `mcp/` (a core→tool layering inversion); `build_configs/` is
neutral and importable by both, like `domain/cost_class_rules.py`.

### Inventory model

`InventoryDoc` gains `build_config: list[BuildConfigDecl]` (`name: str`,
`content: str`, `description: str = ""`). Field validators delegate the config-free checks
(name charset, non-empty content) to `build_configs/rules.py`; a semantic check enforces
name-uniqueness across the list (mirroring `_check_cost_class_uniqueness`). A name/empty
violation raises `InventoryError` at parse, failing the whole reconcile pass for that
iteration (the all-or-nothing file contract). The byte cap is **not** checked here (see
above) — it is enforced deploy-time by `reconcile-systems --check` and at runtime by the
reconcile pass.

### Catalog repository

`build_configs/catalog.py` gains:

- `upsert_config_build_config(conn, name, object_key, sha256, description)` — writes
  `source='config'` unconditionally (the file is authoritative; it clobbers an `operator`
  or `seed` row). Empty description preserves the prior one (the `COALESCE(NULLIF(...))`
  pattern from `upsert_operator_build_config`).
- a `(sha256, source, description)` reader the pass uses for change-detection and drift
  attribution (the seed's `_stored_row` reads `(sha256, source)`; this widens it to include
  `description`, since the change-detection key compares description too — see the reconcile
  pass below). Promoted to the repository and shared with the seed.

### Reconcile pass

`inventory/reconcile_build_configs.py::reconcile_build_configs(conn, doc, store)`:

- Consumes the **same `store` object** the reconcile already threads. Publishing needs
  `put_artifact`, which `ImageHeadStore`/`ImageSweepStore` does not declare but the concrete
  `ObjectStore` the reconciler loop and the on-demand path (with S3 configured) pass does
  have. The pass narrows the store via a `runtime_checkable` publish-capable protocol
  (`head_present` + `put_artifact`): if the store can publish it does; if it cannot (the
  on-demand `_AbsentImageStore` when no S3 is configured) the pass appends a `warned` record
  and leaves every declared row untouched — the same store-down degrade the image pass uses.
  This keeps `reconcile_all`'s call signature unchanged, so **the only M2-gated file the
  change touches is the new migration** (the two `reconcile_all` callers,
  `reconciler/inventory.py` and `mcp/tools/ops/reconcile_systems.py`, are not edited).
- Enforces the byte cap first: a declared fragment whose UTF-8 content exceeds
  `config.require(MAX_BUILD_CONFIG_BYTES)` is appended to `warned` and skipped (row
  untouched), never published.
- Per in-cap declared fragment, under `advisory_xact_lock(BUILD_CONFIG, name)` in its own
  transaction: read `(sha256, source, description)` `FOR UPDATE`. The no-op (change-detecting)
  condition is **all three** of: row exists, `source='config'`, stored sha256 == file sha256,
  **and stored description == file description** — only then write nothing (steady state writes
  nothing, no phantom drift). `description` is in the key because it is a config-owned field the
  file declares: a description-only edit on byte-identical content must still re-assert, or the
  file would declare one description and the catalog keep another (the file-authoritative
  contract would be violated). Otherwise PUT the bytes (when the sha256 changed; a
  description-only change needs no PUT but the spec keeps the publish path simple by
  re-PUTting idempotently to the deterministic key), upsert `source='config'`, and append
  `created` (no prior row) or `updated`. Append `warned` + log the drift line **only** when
  the pass clobbers a meaningful prior — `reconcile_coefficients` warns only on an actual
  value change, never on a creation or no-op, so this mirrors it: warn iff the prior row's
  `source == 'operator'` (a live `buildconfig.set` override being reverted) **or** the prior
  row's `sha256`/`description` differs from the file. A benign adoption — `source='seed'`
  (or absent) → `config` at **identical** content and description — is a `created`/`updated`,
  **not** a `warned`: declaring the existing packaged default into the file clobbered nothing,
  so it must not emit a false "overrode something" signal (which would otherwise inflate the
  on-demand `warned`/audit count on every first-deploy declaration).
- Pruning: none (Decision 2). The pass never deletes a row.

`reconcile_pipeline.reconcile_all` appends `reconcile_build_configs(conn, doc, store)` after
`reconcile_build_hosts` and folds its diff. The `store` parameter's type annotation in
`reconcile_pipeline.py` (an `inventory/` file, not M2-gated) widens to the publish-capable
union; the callers pass the objects they already pass.

## Error handling

| Condition | Outcome |
|-----------|---------|
| Bad `name` / empty `content` | `InventoryError` at parse → whole pass fails this iteration (loud, retried) |
| Over-cap `content` (> `KDIVE_MAX_BUILD_CONFIG_BYTES`) | deploy-time: `reconcile-systems --check` exits non-zero → aborts the upgrade. Runtime (post-deploy growth): per-fragment `warned` skip (row untouched, others still reconcile), not a whole-pass failure |
| Store cannot publish (no S3 / `_AbsentImageStore`) | `warned` record, rows untouched, pass still succeeds |
| Store reachable but PUT raises | the per-fragment transaction rolls back; pass surfaces it like other reconcile failures (logged, retried next pass) |
| PUT succeeds then process/DB crashes before row commit | object holds new bytes, row holds old sha256; `verify_bytes` on `buildconfig.get` / the build fetch raises `INFRASTRUCTURE_FAILURE`; a re-reconcile converges (the #438 fail-closed self-heal) |
| `buildconfig.set` on a declared name | accepted (writes `operator`), then re-asserted to the file value + `warned` on the next reconcile (transient, by design) |

## Testing

Behavior-first, TDD. Tests mirror the package tree.

- **Model** (`tests/inventory/test_model.py`): valid `[[build_config]]`; duplicate name →
  `InventoryError`; bad name, empty content → `InventoryError`; absent block → empty list.
  (The byte cap is **not** a model check — it is asserted in the reconcile-pass tests.)
- **Shared rule** (`tests/build_configs/test_rules.py`): the config-free validator
  (name charset, non-empty) accepts/rejects the same inputs as the `buildconfig.set`
  boundary; the tool is refactored to call it and its existing tests stay green.
- **Validate `--check`** (`tests/inventory/test_reconcile_cli.py`): `validate_systems` exits
  non-zero on an over-cap `[[build_config]]` (deploy-time cap gate) and exits 0 on an in-cap
  fragment; uses the same `MAX_BUILD_CONFIG_BYTES` the reconcile pass reads.
- **Reconcile pass** (`tests/inventory/test_reconcile_build_configs.py`): create a new
  config fragment (publishes + row `source='config'`); change-detecting no-op on an identical
  re-assert; **description-only edit re-asserts** (catalog description updates, no `warned`);
  re-assert over an `operator` row emits `warned` + flips to `config`; **benign adoption** of
  a `seed` row at identical content+description flips to `config` with **no** `warned`;
  adoption of a `seed` row whose bytes differ from the file emits `warned`; over-cap content →
  per-fragment `warned` skip (row untouched, a sibling in-cap fragment still publishes);
  store-cannot-publish degrades to `warned` with rows untouched; removal-from-file leaves the
  row (no prune).
- **Adversarial** (`tests/adversarial/`): concurrent `buildconfig.set` and a reconcile pass
  on the same name serialize on `BUILD_CONFIG` and never commit a row sha256 that mismatches
  the object bytes (the row-vs-object invariant).
- **Seed** (`tests/build_configs/test_seed.py`): a `config`-owned row is skipped by the seed
  (the widened skip condition).
- **Integration** (`tests/integration/`): a full `reconcile_all` over a doc with a
  `[[build_config]]` publishes the fragment and `buildconfig.get` serves the config bytes
  with `source='config'`.
- **M2 gate** (`tests/scripts/test_m2_portability_gate.py`): the meta-test frozenset gains
  `0035`.

## Documentation

- `systems.toml.example`: a commented `[[build_config]]` section explaining inline content,
  the config-authoritative precedence (a declared fragment overrides a live `buildconfig.set`,
  re-asserted on every reconcile), and the no-prune-on-removal contract.
- The generated tool/config reference is regenerated if the change touches advertised
  surfaces (it does not add an MCP tool, but `docs-check`/`config-docs-check` run in CI and
  must stay green).
