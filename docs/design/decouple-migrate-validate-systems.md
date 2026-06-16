# Decouple `migrate()` + deploy-time `systems.toml` validation (#440)

- **Issue:** [#440](https://github.com/randomparity/kdive/issues/440)
- **ADR:** [ADR-0121](../adr/0121-decouple-migrate-validate-systems.md)
- **Status:** design
- **Date:** 2026-06-15

This spec is the design for the decisions formalized in ADR-0121. It does not re-open the
choices settled in that ADR's "Considered & rejected" section.

## Problem

`admin/bootstrap.migrate()` (`src/kdive/admin/bootstrap.py:42-54`) runs three unrelated
concerns in one call:

1. `apply_migrations(conn)` — SQL schema migration.
2. `_reconcile_inventory_images(url)` — reconcile `systems.toml` `[[image]]` → `image_catalog`.
3. `_seed_build_configs_step(url)` — seed packaged build-config fragments into the object
   store + DB.

The Helm chart runs `migrate()` as a Job named "migrate" (`deploy/helm/kdive/templates/
job-migrate.yaml`). Two deploy-safety gaps follow:

- **Dishonest "migrate failed" signal.** A stale/invalid operator `systems.toml` ConfigMap, or
  a missing object-store bucket, fails the "migrate" Job with an opaque Pydantic/`NoSuchBucket`
  traceback after the SQL migration already succeeded — and blocks the whole `helm upgrade`.
- **No validate-only path + inconsistent failure handling.** `reconcile-systems` *writes* and
  *requires* an object store, so it cannot pre-flight-validate a file. The reconciler loop
  degrades on a malformed file (keep-last-good, ADR-0021/0112) while `migrate` hard-fails.

## Acceptance criteria (from the issue)

1. A failed "migrate" Job means SQL migrations actually failed (never config/bucket).
2. An operator can validate `systems.toml` against the current schema using only the image +
   `kubectl`.
3. The chosen failure policy is enforced at deploy time and documented in the k8s runbook.

## Product decision (settled)

A malformed `systems.toml` **fails the deploy fast** (the upgrade aborts with the precise field
error) while the **reconciler loop stays fail-open** at runtime (keep-last-good). The asymmetry
is resolved by *moment of enforcement*, not by unifying policy: deploy time is when nothing yet
runs on the bad file, so a blocking error is cheapest; runtime degrade keeps a live cluster
self-healing. See ADR-0121 "Decision".

## Design

### A. `migrate()` is SQL-only

`migrate(database_url)` applies SQL migrations and returns the applied count. It no longer calls
`_reconcile_inventory_images` or `_seed_build_configs_step`. The now-orphaned
`_reconcile_inventory_images`, `_reconcile_image_store`, and `_NoS3HeadStore` helpers are
removed (the reconciler loop owns continuous inventory reconcile, ADR-0112). `_seed_build_configs_step`
stays (re-homed in B). `register_local_resource`/`seed_demo`/`install_fixtures` are unchanged.

`job-migrate.yaml` drops the `kdive.systemsEnv`/`systemsVolume`/`systemsVolumeMount` includes
(migrate no longer reads `systems.toml`).

**New precondition — baseline `image_catalog` rows come from the reconciler, not migrate.**
Previously the pre-install migrate hook reconciled `systems.toml` `[[image]]` rows *before* the
app rolled out. After this change those config-owned rows are created by the reconciler loop's
first inventory pass, which requires the reconciler to be **deployed, ready (Postgres + object
store healthy — the pass HEADs `s3` images), and not failing-this-pass on a malformed file**.
Two consequences an operator/implementer must expect: (1) a brief post-deploy
eventual-consistency window (≈ one reconcile interval, default 30s) where an immediate
`allocations.request`/build can see no baseline images; (2) a deployment that runs `migrate`
without a reconciler never seeds baseline inventory — correct, since inventory is the
reconciler's job (ADR-0112), but no longer silently covered by migrate.

### B. `seed-build-configs` command + post-deploy hook

- New `__main__` command `seed-build-configs` (handler calls `_seed_build_configs_step` via a
  small public wrapper, e.g. `seed_build_configs_step(database_url) -> int`, exported from
  `admin/bootstrap.py`). S3-gated + idempotent, unchanged behavior.
- New `deploy/helm/kdive/templates/job-seed-build-configs.yaml`: a `post-install`/`post-upgrade`
  hook with `helm.sh/hook-weight: "10"` (after migrate, which is weight `0` — relevant on the
  bundled path where migrate is also `post-*`), `envFrom` the config ConfigMap (DB + S3). On the
  bundled path it reuses the `wait-for-db` initContainer pattern. No systems volume.

**Failure semantics (deliberate — honest signal, not silent skip).** The seed is *not* made
unconditionally best-effort. `_seed_build_configs_step` skips cleanly **only** when S3 is
*wholly unconfigured* (it catches `CONFIGURATION_ERROR` from `object_store_from_env()` — the env
is absent). A *configured-but-broken* object store (missing bucket, wrong credentials) does
**not** degrade: `object_store_from_env()` succeeds (it never HEADs the bucket), then
`seed_build_configs → put_artifact` raises an uncaught `NoSuchBucket`/`AccessDenied`, failing
the hook. This is intentional — a real object-store fault must surface, not be swallowed (the
"no silent failures" rule). Because the hook is `post-*`, on the external path it runs *after*
the app pods roll out, so a seed failure yields a Job named `*-seed-build-configs` failing while
the server/worker/reconciler are already up. The operator recovers by fixing the object store and
either re-running `helm upgrade` (which re-fires the `post-upgrade` seed hook) or running
`python -m kdive seed-build-configs` in a pod carrying the config ConfigMap env (the chart ships
no CronJob to clone). Build-config fragments are only consumed at build time, so a delayed seed
never breaks a running control plane. This partial-failure state is documented in the k8s runbook
(AC#3) so "release failed, pods healthy" is not surprising.

### C. `reconcile-systems --check`

`_add_reconcile_systems_arguments` gains `--check` (store_true). `_handle_reconcile_systems`
branches on it: with `--check`, it loads + validates the resolved file via the existing
`_load_doc(path)` and exits — **no** `object_store_from_env()`, **no** `create_pool()`. Exit
codes:

- valid file, or absent **default** path → `0`
- malformed/invalid file → `1` (the `InventoryError` message `entry.field: msg` to stderr)
- explicit `--path` to a missing file → `1`, with the `InventoryError` `cannot read` message that
  **names the path** (`<path>.file: cannot read: …`, from `load_inventory`). The validate hook
  always passes an explicit `--path` to the mounted file, so this same path-naming message covers
  the "ConfigMap mounted but its key did not match `systems.fileName`" case (the mount is then
  empty). `validate_systems` cannot tell that case apart from a CLI typo — a bare path carries no
  intent — so it does **not** synthesize a different message; the path-naming error is actionable
  for both, and the configMapName-must-equal-`fileName` requirement is explained in the runbook
  (see D / AC#3).

The check path is implemented in `reconcile_cli.py` as `validate_systems(path) -> int` so the
CLI handler stays thin and the logic is unit-testable without argparse.

### D. Fail-fast pre-upgrade hook

New `deploy/helm/kdive/templates/job-validate-systems.yaml`:

- `helm.sh/hook: pre-install,pre-upgrade`, `helm.sh/hook-weight: "-10"` (before the config
  ConfigMap at `-5` and migrate at `0`), `helm.sh/hook-delete-policy: before-hook-creation`.
- Rendered only `{{- if .Values.systems.configMapName }}` — no inventory file, no hook.
- Mounts the systems ConfigMap (`kdive.systemsVolume`/`systemsVolumeMount`) and sets
  `KDIVE_SYSTEMS_TOML` (`kdive.systemsEnv`).
- `args: ["reconcile-systems", "--check", "--path", "<mountPath>/<fileName>"]`.
- Needs **no** DB or S3 — `--check` touches neither, so no `wait-for-db` initContainer and no
  config-ConfigMap dependency.

A malformed file fails this hook; Helm aborts before migrate and before app rollout.

**ConfigMap preconditions (operator-owned resource).** The systems ConfigMap is referenced **by
name** (`.Values.systems.configMapName`) and is created out-of-band by the operator — the chart
never renders it. The hook therefore inherits two preconditions, both shared with the existing
migrate-job mount:

- *The named ConfigMap must exist.* A `configMapName` pointing at a non-existent ConfigMap leaves
  the hook pod in `CreateContainerConfigError` until the hook timeout — an opaque blocking
  failure (the same class as issue #311 for the config ConfigMap). This is pre-existing for
  `job-migrate.yaml`, but the validate hook now makes it gate the upgrade *earlier*. Documented in
  the runbook as a checked precondition, not silently assumed.
- *The ConfigMap key must equal `systems.fileName`.* `kdive.systemsVolume` projects
  `items: [{key: fileName, path: fileName}]`; a key/`fileName` mismatch yields an empty mount, so
  `--check --path <mountPath>/<fileName>` hits a missing file and exits 1 with the path-naming
  `cannot read` message (see C). The validator cannot infer the configMapName/fileName cause from
  a bare path, so the runbook documents this requirement as a checked precondition.

**Where the error surfaces (AC#2 observability).** The `InventoryError` field message goes to the
validate-hook pod's stderr. A failed Helm hook reports only "pre-upgrade hooks failed", not the
pod logs, so the operator reads the precise error with
`kubectl logs job/<release>-kdive-validate-systems`. They must read it **before** retrying the
upgrade: the `before-hook-creation` delete policy reaps the failed pod when the next `helm
upgrade` recreates the hook. The runbook states this as the AC#2 validation step.

## Edge / failure cases

| Input | `migrate` | `reconcile-systems --check` / validate hook | `seed-build-configs` hook | reconciler (unchanged) |
|---|---|---|---|---|
| Valid `systems.toml` | SQL only | exit 0 / pass | seeds (≤1 row) or skip | reconcile |
| Malformed `systems.toml` | SQL only (unaffected) | exit 1 + field error / **abort upgrade** | not reached (pre-upgrade hook aborted first) | fail-this-pass, keep-last-good |
| Absent default path | SQL only | exit 0 / hook not rendered (no ConfigMap) | seeds or skip | no-op |
| ConfigMap missing / key ≠ `fileName` | SQL only | exit 1 (named-path error) or `CreateContainerConfigError` / **abort upgrade** | not reached | n/a |
| No S3 configured (env absent) | SQL only | exit 0 (S3 not touched) | **skips cleanly** (`CONFIGURATION_ERROR` caught) | s3 images stay `defined` |
| S3 configured, bucket missing / bad creds | SQL only (unaffected) | exit 0 (S3 not touched) | **hook FAILS** (`put_artifact` raises, uncaught) → release failed, app pods up; recover by fixing S3 + re-running `seed-build-configs` | reconcile pass fails (logged, keep-last-good) |

## Out of scope

- The reconciler loop's runtime degrade behavior (ADR-0021/0112) is unchanged.
- No schema change, migration, or new MCP tool.
- The `ops.reconcile_systems` MCP tool is unchanged (it is the on-demand write trigger, not a
  validator).

## Test plan (behavior, at the boundary)

- `migrate()` is SQL-only: with a `systems.toml` + S3 present, migrate applies the schema and
  creates **no** `image_catalog` config rows and **no** `build_config_catalog` rows (rewrites
  the four existing `test_bootstrap.py` migrate tests).
- `seed-build-configs`: seeds with S3 (fake store), skips cleanly when S3 env is **absent**
  (returns 0), idempotent on re-run, and **propagates** (does not swallow) a non-configuration
  object-store error — a store whose `put_artifact` raises a non-`CONFIGURATION_ERROR` must
  surface, asserting the honest-failure semantics.
- `validate_systems(path)`: returns 0 on a valid baseline file, 0 on an absent default path,
  1 + the `InventoryError` field message on a malformed file, and 1 with a path-naming message
  on an explicit missing `--path` (the mounted-file-absent case); and it acquires no pool/store
  (assert via a guard that would raise if S3/DB were touched).
- Helm: `helm template` renders `job-validate-systems.yaml` only when `systems.configMapName`
  is set, with weight `-10` and the `--check` args; `job-migrate.yaml` carries no systems
  volume; `job-seed-build-configs.yaml` renders as a `post-*` hook. (Reuse the existing
  docker/helm-render test harness.)
