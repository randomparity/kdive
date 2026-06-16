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

### B. `seed-build-configs` command + post-deploy hook

- New `__main__` command `seed-build-configs` (handler calls `_seed_build_configs_step` via a
  small public wrapper, e.g. `seed_build_configs_step(database_url) -> int`, exported from
  `admin/bootstrap.py`). S3-gated + idempotent, unchanged behavior.
- New `deploy/helm/kdive/templates/job-seed-build-configs.yaml`: a `post-install`/`post-upgrade`
  hook, weighted after migrate, `envFrom` the config ConfigMap (DB + S3). On the bundled path it
  reuses the `wait-for-db` initContainer pattern. No systems volume. Its failure is named
  `*-seed-build-configs`.

### C. `reconcile-systems --check`

`_add_reconcile_systems_arguments` gains `--check` (store_true). `_handle_reconcile_systems`
branches on it: with `--check`, it loads + validates the resolved file via the existing
`_load_doc(path)` and exits — **no** `object_store_from_env()`, **no** `create_pool()`. Exit
codes:

- valid file, or absent default path → `0`
- malformed/invalid file, or explicit `--path` to a missing file → `1` (the `InventoryError`
  message `entry.field: msg` to stderr)

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

## Edge / failure cases

| Input | `migrate` | `reconcile-systems --check` | validate hook | reconciler (unchanged) |
|---|---|---|---|---|
| Valid `systems.toml` | SQL only | exit 0 | pass | reconcile |
| Malformed `systems.toml` | SQL only (unaffected) | exit 1 + field error | **abort upgrade** | fail-this-pass, keep-last-good |
| Absent default path | SQL only | exit 0 | hook not rendered (no ConfigMap) | no-op |
| Explicit `--path` missing | n/a | exit 1 | n/a | n/a |
| No S3 configured | SQL only | exit 0 (S3 not touched) | pass (S3 not touched) | s3 images stay `defined` |
| Missing bucket | SQL only (unaffected) | exit 0 (S3 not touched) | pass | seed/reconcile degrade |

## Out of scope

- The reconciler loop's runtime degrade behavior (ADR-0021/0112) is unchanged.
- No schema change, migration, or new MCP tool.
- The `ops.reconcile_systems` MCP tool is unchanged (it is the on-demand write trigger, not a
  validator).

## Test plan (behavior, at the boundary)

- `migrate()` is SQL-only: with a `systems.toml` + S3 present, migrate applies the schema and
  creates **no** `image_catalog` config rows and **no** `build_config_catalog` rows (rewrites
  the four existing `test_bootstrap.py` migrate tests).
- `seed-build-configs`: seeds with S3 (fake store), skips cleanly without S3, idempotent on
  re-run.
- `validate_systems(path)`: returns 0 on a valid baseline file, 0 on an absent default path,
  1 + the `InventoryError` field message on a malformed file, 1 on an explicit missing `--path`,
  and acquires no pool/store (assert via a guard that would raise if S3/DB were touched).
- Helm: `helm template` renders `job-validate-systems.yaml` only when `systems.configMapName`
  is set, with weight `-10` and the `--check` args; `job-migrate.yaml` carries no systems
  volume; `job-seed-build-configs.yaml` renders as a `post-*` hook. (Reuse the existing
  docker/helm-render test harness.)
