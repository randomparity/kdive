# ADR 0121 — Decouple `migrate()` to SQL-only + deploy-time `systems.toml` validation (#440)

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0112](0112-systems-inventory-config.md) (the
  `systems.toml` inventory engine + the reconciler loop's continuous inventory reconcile),
  [ADR-0021](0021-reconciler-loop-drift-repair.md) (the reconciler's keep-last-good degrade on
  a malformed file at runtime — left intact), [ADR-0096](0096-kdump-config-fragment-build-input.md)
  (the S3-gated build-config seed this re-homes out of `migrate()`),
  [ADR-0088](0088-deployment-packaging.md) (the Helm chart + migrate-as-hook packaging),
  [ADR-0015](0015-sql-migration-runner.md) (the SQL migration runner).
- **Theme:** third instance of the #428 dev/operator-separation theme (the fleet-config
  layer), after [ADR-0119](0119-operator-build-config-write-path.md) (#438) and
  [ADR-0120](0120-operator-fixture-profile-write-path.md) (#439).
- **Issue:** [#440](https://github.com/randomparity/kdive/issues/440).
- **Spec:** [`../design/decouple-migrate-validate-systems.md`](../design/decouple-migrate-validate-systems.md).

## Context

`admin/bootstrap.migrate()` runs three unrelated concerns in one call: apply SQL migrations →
reconcile `systems.toml` `[[image]]` entries into `image_catalog` (`_reconcile_inventory_images`,
ADR-0112) → seed the packaged build-config fragments into the object store + DB
(`_seed_build_configs_step`, ADR-0096). The Helm chart runs this as a Job **named "migrate"**
(`job-migrate.yaml`), a `pre-install`/`pre-upgrade` hook on the external-backend path.

Two coupled deploy-safety gaps surfaced during the 2026-06-15 k8s `:edge` refresh
([[k8s-deploy-edge-refresh]]):

1. **The "migrate failed" signal is dishonest.** A stale/invalid operator `systems.toml`
   ConfigMap, or a missing object-store bucket, fails the Job named "migrate" with an opaque
   Pydantic/`NoSuchBucket` traceback — *even though the SQL schema migration already
   succeeded*. The operator reads "migrate failed" and looks at the database; the fault is in
   config or object storage. The fused steps also make a config/bucket error **block the whole
   `helm upgrade`** at the migrate hook.

2. **No validate-only path, and inconsistent failure handling.** The in-image
   `reconcile-systems` command *writes* to the catalog and *requires* an object store (it HEADs
   `s3` image objects), so it cannot serve as a pre-flight validator. Meanwhile the reconciler
   loop treats a malformed `systems.toml` as fail-this-pass + keep-last-good (degrade, ADR-0021)
   while `migrate` **hard-fails** — the same input, opposite behavior, with no operator-facing
   way to validate a candidate file before relying on it. `#432` (making `vcpus`/`memory_mb`
   required) surfaced ConfigMap drift only at migrate time.

The `kdive-systems` ConfigMap is operator-owned by name (`systems.configMapName`), so it drifts
from the source file; today's only mitigation is a manual `kubectl` re-sync, with no guardrail.

## Decision

Split the three concerns so the "migrate" name means **only SQL**, re-home the build-config
seed to its own command + hook, and add a **deploy-time fail-fast** `systems.toml` validator
that runs before migrate — while leaving the reconciler's **runtime fail-open** behavior intact.
The asymmetry between "migrate hard-fails" and "reconciler degrades" is resolved by **moment of
enforcement**, not by making the two policies identical:

- **Deploy time** (the operator is pushing a config change) → **fail-fast**: abort the upgrade
  with the precise field error before any new config or schema change takes effect. This is the
  right moment to refuse: nothing is running on the bad config yet.
- **Runtime** (the reconciler loop) → **fail-open** (unchanged): a malformed file fails *that
  pass* and keeps the last-good document, because aborting the reconciler would stop all the
  other drift repairs (orphan teardown, lease reclaim). A live cluster must not stop self-healing
  because someone fat-fingered an edit.

### 1. `migrate()` is SQL-only

`migrate()` applies SQL migrations and returns the applied count — nothing else. The inventory
reconcile and build-config seed calls are removed from it. AC#1 holds by construction: a failed
"migrate" Job means a SQL migration actually failed.

The migrate-time **inventory reconcile is dropped, not re-homed.** The reconciler loop already
reconciles `systems.toml` into the catalog continuously (ADR-0112, `reconcile_inventory` pass);
the migrate-time call was a redundant "baseline before first build" convenience, and the
reconciler's first pass (within one ~30s interval of startup) re-establishes that baseline.
`_reconcile_inventory_images`, `_reconcile_image_store`, and `_NoS3HeadStore` are removed from
`bootstrap.py`.

The `job-migrate.yaml` Job stops mounting the systems ConfigMap and stops setting
`KDIVE_SYSTEMS_TOML` (migrate no longer reads `systems.toml`).

### 2. Build-config seed → its own command + post-deploy hook

`_seed_build_configs_step` is unchanged (S3-gated, idempotent, ADR-0096) but is now invoked by a
new operator command, `kdive seed-build-configs`, instead of from `migrate()`. The Helm chart
gains `job-seed-build-configs.yaml`, a `post-install`/`post-upgrade` hook that runs after migrate
and after the DB exists. Its failure is a Job named `*-seed-build-configs`, never "migrate" — so
a genuine object-store error is still surfaced, under an honest name, without blocking the schema
migration or implying the database is broken. The seed is a clean no-op **only** when S3 is
*wholly unconfigured* (env absent, `CONFIGURATION_ERROR`); a *configured-but-broken* store
(missing bucket, bad credentials) is not silently skipped — it raises and fails the
`*-seed-build-configs` hook (the "no silent failures" rule). Because this hook is `post-*`, a
seed failure can leave the release marked failed while the app pods are already healthy; the
operator recovers by fixing the object store and re-running `seed-build-configs`. This
partial-failure state is documented in the k8s runbook.

### 3. `reconcile-systems --check` — a validate-only mode (no DB/S3)

`reconcile-systems` gains a `--check` flag. With `--check`, the command parses and
schema-validates the resolved `systems.toml` and exits — it acquires **no** object store and
opens **no** Postgres pool, and writes nothing. It exits `0` on a valid file (and on an absent
default path — the gitignored pre-config state), and non-zero with the precise
`InventoryError` field message (`entry.field: msg`) on a malformed/invalid file. An explicit
`--path` to a missing file is an operator error (non-zero), matching the existing
`load_inventory`/`load_inventory_optional` split. This reuses the command's existing path
resolution; `--check` short-circuits before the store/pool are touched.

### 4. Fail-fast pre-upgrade Helm hook

The chart gains `job-validate-systems.yaml`, a `pre-install`/`pre-upgrade` hook **weighted
before migrate** (weight `-10`; migrate is `0`, the config ConfigMap is `-5`). It mounts the
operator's systems ConfigMap and runs `reconcile-systems --check --path <mounted file>`. It is
rendered only when `.Values.systems.configMapName` is set (no inventory file → nothing to
validate → no hook). A malformed file fails this hook, and Helm aborts the upgrade **before**
migrate runs and before the new app pods roll out — production-reachable, using the deployed
image, no `justfile`. The reconciler's runtime degrade path is untouched.

## Consequences

- A failed "migrate" Job now unambiguously means SQL migration failure (AC#1). Config/bucket
  faults surface as `*-validate-systems` (fail-fast, pre-upgrade) or `*-seed-build-configs`
  (post-upgrade) — honest names.
- An operator can validate `systems.toml` against the current schema with only the image +
  `kubectl` (`reconcile-systems --check`), with no DB or object store reachable (AC#2).
- The chosen failure policy (fail-fast at deploy) is enforced at deploy time by the pre-upgrade
  hook and documented in the k8s runbook (AC#3).
- Three Job templates exist where one did (migrate, validate-systems, seed-build-configs). This
  is the cost of an honest per-concern failure signal; each Job has a single responsibility and
  a name that means what it fails on.
- The migrate-time inventory baseline now depends on the reconciler's first pass rather than the
  migrate hook. On a fresh install the reconciler reconciles within one interval; a deployment
  that runs migrate without a reconciler (a bare schema-only bring-up) no longer auto-seeds
  baseline images — which is correct, since inventory is the reconciler's job (ADR-0112).
- No schema change, no migration, no new MCP tool, no auth-model change.

## Considered & rejected

- **Keep the fused steps but catch and swallow their errors in `migrate()`.** Rejected: it
  makes "migrate" *succeed* while the operator's config or object store is broken, hiding a real
  fault until a later build/allocation fails confusingly. The goal is an honest signal, not a
  suppressed one.
- **Fail-open at deploy time (never block the upgrade; surface via doctor/health).** Rejected
  per the product decision on #440: a silently-degraded deploy hides operator config errors
  until a later operation fails with a confusing symptom. Deploy time is exactly when a clear,
  blocking error is cheapest to act on. (The reconciler stays fail-open at *runtime* — that is a
  different moment, see Decision.)
- **Make the reconciler also fail-fast (unify the policy).** Rejected: aborting the reconciler
  loop on a malformed file would stop orphan teardown, lease reclaim, and every other drift
  repair on a running cluster. Keep-last-good is correct at runtime (ADR-0021/0112). The
  apparent inconsistency is resolved by enforcing fail-fast at the deploy boundary, where
  nothing is yet running on the bad file.
- **A brand-new `kdive config validate` command instead of `reconcile-systems --check`.**
  Rejected: it would duplicate the path-resolution logic (`KDIVE_SYSTEMS_TOML` default vs
  explicit `--path`, absent-default-is-no-op) that already lives in `reconcile-systems`.
  `--check` is a natural read-only mode of the same command, sharing that resolution and simply
  short-circuiting before the store/pool are acquired.
- **Validate inside the migrate Job as a pre-step.** Rejected: a validation failure inside the
  "migrate" Job still reads as "migrate failed", re-introducing the dishonest signal. AC#1
  requires the validation failure to be a distinct Job with its own name.
- **Re-home the inventory reconcile into its own hook (parallel to the build-config seed
  hook).** Rejected: the reconciler already reconciles inventory continuously (ADR-0112); a
  migrate-time or hook reconcile would duplicate it and re-introduce a second writer of the same
  rows. The build-config seed, by contrast, has no other home, so it does get a hook.
