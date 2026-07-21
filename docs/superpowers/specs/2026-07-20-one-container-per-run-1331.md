# Spec — One backend container per test run, database-per-worker (#1331)

- **Issue:** #1331 — "Speed up `just test`: one Postgres/MinIO container per run,
  not per xdist worker"
- **ADR:** [0401](../../adr/0401-one-backend-container-per-run.md)
- **Date:** 2026-07-20
- **Status:** Draft

## Problem

`just test` runs `pytest -n auto` (~18 workers). The `postgres_url`
(`tests/db/conftest.py:23`) and `minio_store` (`tests/store/conftest.py:55`)
fixtures are `scope="session"`, but under pytest-xdist "session" means *per worker
process*, so ~18 Postgres and ~18 MinIO containers start (each ~3s) and stop
(serial `container.stop()` tail of 4–8s each) per run. Measured recoverable time:
~15–25s of a ~64s run.

## Goal

One Postgres container and one MinIO container **per run**, each worker still fully
isolated, with the `KDIVE_REQUIRE_DOCKER=1` hard-fail contract intact. No new
runtime dependency; no external backend required for the default `just test` or CI.

## Non-goals

- Changing the per-test reset contract (`pg_conn`'s `DROP SCHEMA public`, `key_ns`'s
  per-test prefix). Those stay; they now operate inside a per-worker namespace.
- `--dist worksteal` (#1332) and truncate-based reset (#1333). Separate issues; this
  one is a prerequisite for #1333.
- Windows support for the db/store suites (POSIX-only, pre-existing).

## Design summary (see ADR-0401 for the decision and rejected alternatives)

Two source fixtures change: `postgres_url` (`tests/db/conftest.py`) and `minio_store`
(`tests/store/conftest.py`). Both are re-exported by ~19 conftests, so every consumer
inherits the new behavior. Each fixture, in order:

1. **Env override.** If `KDIVE_TEST_PG_URL` (resp. `KDIVE_TEST_S3_URL` +
   `KDIVE_TEST_S3_ACCESS_KEY` / `KDIVE_TEST_S3_SECRET_KEY`, defaulting to
   `minioadmin`) is set, treat it as the shared **server** and start no container.
2. **Else, one shared container, lazily started, refcounted** across xdist workers
   via a `fcntl.flock` guard + JSON state file in the **per-run** temp root — the
   xdist-shared `tmp_path_factory.getbasetemp().parent` under `-n`, else
   `getbasetemp()` itself (under non-xdist, `.parent` is the *persistent* per-user
   root and must not be used). First worker starts the container and sets
   `refcount=1`; others increment; the worker that decrements to `0` stops and removes
   it by recorded container id. Ryuk disabled for the run. The shared Postgres is
   started with a raised `max_connections` sized to workers × pool. The guaranteed
   invariant is **at most one container of each kind at any instant**, not a hard
   single start per run (see AC1).
3. **Per-worker, run-unique namespace.** Provision `kdive_test_<worker>_<token>`
   database (resp. `kdive-test-<worker>-<token>` bucket) on the shared server; yield
   its conninfo (resp. an `ObjectStore` bound to it). Drop it on per-worker teardown.
   `<token>` is a short `uuid4` hex minted by **each worker** at fixture setup. Because
   the per-worker database name is consumed only by the worker that creates and drops
   it (no cross-worker reference), the token need not be shared across a run's workers;
   a per-worker uuid is globally unique, so two runs sharing one **override** backend —
   even on different hosts — never collide on a name (the container path is already
   run-isolated by its fresh container). The `<worker>` component stays for readability
   so a leaked namespace names its owner.

Worker id: `PYTEST_XDIST_WORKER` env, or `master` when not under xdist.

The flock + refcount + worker-id + stop-by-id coordination is factored into one test
support helper (`tests/support/xdist_backend.py`) that both fixtures call; the
resource-specific start/provision callbacks stay in each conftest.

## Acceptance criteria

Each is a checkable behavior; `/build-tdd` implements these as tests where the harness
allows, and the container-count criteria are verified by a live `just test` run.

- **AC1 — at most one container of each kind concurrently under xdist.** A
  `just test -n <N>` run (N>1) with no env override has at most one Postgres and one
  MinIO container alive at any instant — the state file records a single
  `container_id` while N workers hold it — and for the full suite that is a single
  start and single stop, not N of each. The mechanism guarantees *at-most-one-
  concurrent*, not a hard "exactly one start" (a sparse/clustered DB schedule that
  drains all current holders before a later worker's first DB test may stop and
  lazily restart one — correctness-neutral, costs a repeated start). *Verify (unit):* a
  coordination test drives an acquire×K / release×K sequence **and** a
  finish-early-then-reacquire ordering against a fake container, asserting one live
  container while any holder is active and stop called exactly at `refcount == 0`.
  *Verify (real, Docker-gated):* a test that exercises the helper with **real**
  testcontainers from two concurrent acquirers sharing one temp root asserts exactly
  one real container is started (queried by the recorded id) and is stopped/removed
  after the last release — so the real start/stop/stop-by-id plumbing, not only the
  fake, is falsifiable in CI (skips without Docker, hard-fails under
  `KDIVE_REQUIRE_DOCKER=1` like the other backend tests).
- **AC2 — per-worker isolation preserved.** Two xdist workers running DB tests
  concurrently do not observe each other's schema/rows; `pg_conn`'s `DROP SCHEMA
  public CASCADE` in worker A never affects worker B. *Verify:* the full existing db
  suite passes green under `-n auto`; a coordination test asserts distinct database
  names per worker id.
- **AC3 — env override is honored and starts no container.** With
  `KDIVE_TEST_PG_URL` / `KDIVE_TEST_S3_URL` set to a running backend, the fixtures
  connect to it, create per-worker databases/buckets, and start no testcontainer.
  *Verify:* a test invokes the fixture's server-selection with the env set and asserts
  the container-start path is not taken (monkeypatched/spy) and the returned conninfo
  points at the override host with a `kdive_test_<worker>_<token>` path (worker id
  followed by the uuid token).
- **AC4 — provisioning never fails on a prior run's leftovers.** Against a persistent
  (override) backend, a new run never collides with a prior run's namespace (the uuid
  token differs), so provisioning succeeds without an "already exists" error; and a
  same-token re-provision (a simulated crash-leftover retry) reclaims cleanly via
  `DROP DATABASE IF EXISTS … (FORCE)` / empty-bucket. *Verify:* one test provisions two
  distinct-token namespaces against the same server and asserts both succeed
  independently; a second asserts that re-provisioning the *same* `kdive_test_<worker>_<token>`
  name drops-and-recreates without error. (Because uuid names never recur in normal
  operation, this `IF EXISTS … FORCE` guards only a same-token retry, not cross-run
  crash recovery — that is the required operator sweep in the edge-case section.)
- **AC5 — `KDIVE_REQUIRE_DOCKER` contract intact.** With Docker unavailable and no
  override: unset → the fixture skips; `KDIVE_REQUIRE_DOCKER=1` → it hard-fails
  (re-raises), on both the import-guard and container-start failure paths. *Verify:*
  the existing require-docker behavior tests still pass; extend them to cover the
  shared-start path.
- **AC6 — refcount teardown stops the container exactly once.** When the last worker
  releases the fixture, the container is stopped and the state file removed; earlier
  releases do not stop it. *Verify:* a coordination test drives acquire×K / release×K
  against a fake container object and asserts stop is called once, on the K-th
  release, and never before.
- **AC6b — shared server sized for the actual worker count.** The shared Postgres does
  not exhaust connections under `-n auto`, which scales to core count (not a fixed ~18).
  The container starts with `max_connections = max(500, worker_count × 20)`, where
  `worker_count` is `PYTEST_XDIST_WORKER_COUNT` (treated as 1 when unset) and `20` is
  `create_pool` default `max_size` (10) × 2 headroom — so a 64-core host (64 workers →
  1280) is covered, while smaller hosts keep the 500 floor. The `just compose-up`
  Postgres (the override backend) is bumped to the 500 floor (its ceiling is the
  operator's to raise for very large `-n`). *Verify:* a full `-n auto` db-suite run does
  not raise `FATAL: too many clients`; the started container's `max_connections` is
  asserted `>=` the computed bound for the run's worker count.
- **AC7 — measured speedup.** A local `just test` run is meaningfully faster than
  before (target: the container start/stop tail visible in `--durations` collapses
  from ~20 `~3s setup` + a serial `stop` tail to a single start + single stop).
  *Verify:* record `--durations` wall time before/after in the PR description; this is
  evidence, not a CI gate.
- **AC8 — guardrails green.** `just ci` (lint, type, lint-shell, lint-workflows,
  check-mermaid, test) passes. No new runtime dependency added.

## Edge cases and failure modes

- **Two workers race to be first.** `fcntl.flock` serializes the read-modify-write of
  the state file; exactly one sees an absent/`refcount==0` state and starts the
  container.
- **Creator worker finishes first.** Ryuk disabled + refcount teardown means the
  container survives until the *last* worker releases, regardless of completion order.
- **Worker SIGKILLed before teardown.** Refcount never reaches 0; the single shared
  container leaks for the run. Bounded (one, not 18); documented; cleaned by
  `docker container prune`. Not silently retried into reuse.
- **Stale state file from a crashed prior run (container path).** Not reachable: the
  coordination state file lives in the **per-run** temp root (a fresh `pytest-N`
  directory each run; pytest keeps only the last few and never reuses N for a live
  run), so a crashed prior run's state file sits in a *different* root that this run
  never reads. No stale-container detection or `is_alive` check is needed — the state
  file a run sees is always its own.
- **Pre-existing per-worker database/bucket (override path).** Setup drops-and-creates
  the database (`DROP DATABASE IF EXISTS … (FORCE)`) and empties-or-creates the
  bucket, so a prior run's leftovers are reclaimed.
- **Override server lacks CREATEDB / bucket-create rights.** Fails loudly at
  provisioning — the correct signal, not a silent skip.
- **Non-xdist run (`pytest` without `-n`).** Worker id `master`; the coordination
  root is `getbasetemp()` (per-run), **not** `.parent` (which is the persistent
  per-user root under non-xdist). The flock/refcount path runs with a single holder
  (start → refcount 1 → teardown → stop). One container, as today.
- **Two concurrent non-xdist runs.** Because the non-xdist root is per-run
  (`getbasetemp()`), a developer running `pytest tests/db` in two terminals — or
  pre-commit plus a manual run — gets two independent per-run roots and two
  independent containers, not a shared/refcount collision on a global lock. (Using
  `.parent` under non-xdist would have collided; AC-covered by the root-selection
  logic.)
- **Two concurrent runs against one override backend.** The per-worker `uuid4` token
  (design step 3) keeps any two runs pointed at the same `KDIVE_TEST_PG_URL` — same
  host or different — from colliding: each worker provisions its own
  `kdive_test_<worker>_<token>` database, so run A's `DROP DATABASE … (FORCE)` never
  targets run B's in-use database (the names differ globally). *Residual:* a crashed
  run leaves its uuid-named databases/buckets on a *persistent* override backend (the
  container path has no leftover — the container is gone), and because uuid names never
  repeat they are not reclaimed by name-reuse. This is **bounded per crash
  (~worker_count namespaces) but unbounded in aggregate without cleanup** — orphans
  accumulate monotonically across crashed runs. No age-based auto-sweep is added
  (dropping by age on a *shared* backend could drop a concurrent long-running run's
  database — a worse footgun than the leak). Instead, periodic cleanup of the
  self-describing `kdive_test_*` / `kdive-test-*` prefix on a persistent override
  backend (a prefix `DROP DATABASE` sweep, or recreating the compose volume) is a
  **required** operator task, documented in the fixture docstrings and the compose
  runbook — not an optional remedy. The default container path needs none of this.
- **Shared connection ceiling.** One server now holds every worker's pool. The shared
  container is started with a raised `max_connections`; the override backend must be
  sized likewise (documented; `just compose-up` bumped). Without this, `-n auto` can
  intermittently raise `FATAL: too many clients` on high-core hosts (a flake absent
  under container-per-worker).

## Change set

- `tests/db/conftest.py`, `tests/store/conftest.py` — the two fixtures.
- `tests/support/xdist_backend.py` (new) — the flock/refcount/worker-id/run-token
  coordination helper.
- New coordination + real-container tests (AC1, AC6b, AC4, AC5).
- `docker-compose.yml` / `justfile` — bump the compose Postgres `max_connections` to
  the 500 floor so an override run does not exhaust it (a standalone, safe-to-leave
  config change).
- ADR-0401 and this spec — design records.

## Rollback

Test-infra + one compose-config bump; no schema, migration, or runtime `src` code.
Reverting the two conftest edits, the helper, and the new tests restores
container-per-worker with no data or API impact. The `docker-compose.yml`
`max_connections` bump is independent and safe to leave in place (it only widens a
local dev backend's ceiling); revert it too for a full undo.
