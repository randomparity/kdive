# ADR 0400 — One Postgres/MinIO backend container per test run, database-per-worker

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-20
- **Deciders:** D. Christensen (core platform)

## Context

`just test` runs the unit/service suite with `pytest -n auto` (pytest-xdist). The
disposable-backend fixtures — `postgres_url` (`tests/db/conftest.py`, ADR-0015) and
`minio_store` (`tests/store/conftest.py`, ADR-0017) — are `scope="session"`. Under
xdist, "session" scope is **per worker process**, so each of the ~18 workers on a
typical host starts its *own* Postgres and MinIO container and stops it at end of
run.

The forces:

- **Container churn dominates wall time, not the tests.** `--durations` on a full
  `just test` shows ~20 `~3s setup` entries at the container layer and a serial
  teardown tail of `4–8s` `container.stop()` finalizers. Estimated ~15–25s of a
  ~64s run is container start/stop, not test work (#1331).
- **Isolation is currently a free side effect of one-container-per-worker.** Each
  worker owns a whole database, so `pg_conn`'s per-test `DROP SCHEMA public CASCADE`
  and `minio_store`'s shared bucket never collide across workers. Collapsing to one
  shared container removes that free isolation, so the isolation boundary must move
  from *container* to *database/bucket*.
- **CI has no shared backend to point at.** CI runs `just test` with
  `KDIVE_REQUIRE_DOCKER=1` against Docker-on-the-runner via testcontainers — there
  are no compose service containers and no env override. A speedup that only lands
  when an external backend is supplied would not help CI or a bare `just test`.
- **`KDIVE_REQUIRE_DOCKER=1` hard-fail must survive.** The skip-when-no-Docker →
  hard-fail semantics (ADR-0015/0017) keep a broken runner from masking the schema
  and store suites; the new mechanism must not weaken it.
- **The reaper's process assumption.** testcontainers starts a Ryuk reaper tied to
  the *process* that created a container; when that process exits, Ryuk reaps the
  container after a delay. A container shared across worker processes and created
  lazily by whichever worker asks first would be reaped out from under the others
  the moment its creator finishes.
- **DB-less test subsets must stay cheap.** `pytest tests/domain/…` and the
  inner-loop recipes (#1334) must not pay for a Postgres/MinIO start they never use.
  The mechanism must remain *lazy* — a backend starts only when a test actually
  requests it.

This ADR pins the coordination mechanism. It does not restate the schema/bucket
reset contract (the fixtures' `pg_conn` / `key_ns` still own that) nor re-argue
Postgres/MinIO as backends ([0015](0015-sql-migration-runner.md),
[0017](0017-object-store-client-interface.md)).

## Decision

Move from **container-per-worker** to **one backend container per run, one database
(and one bucket) per worker**, coordinated by the workers themselves so no external
backend is required.

1. **Env override, first.** `postgres_url` reuses a server named by
   `KDIVE_TEST_PG_URL` when set; `minio_store` reuses one named by
   `KDIVE_TEST_S3_URL` (credentials `KDIVE_TEST_S3_ACCESS_KEY` /
   `KDIVE_TEST_S3_SECRET_KEY`, defaulting to the `just compose-up` `minioadmin`
   values). When an override is set, no container is started. This is the CI/compose
   escape hatch and makes the fixtures trivially reusable against a long-lived
   backend.

2. **Otherwise, one lazily-started shared container, refcounted.** With no override,
   the fixtures coordinate through the xdist-shared temp root
   (`tmp_path_factory.getbasetemp().parent`, shared across workers) using a stdlib
   `fcntl.flock` guard and a small JSON state file (`{url, container_id, refcount}`):
   - The first worker to request the fixture starts the container, records its
     connection URL and wrapped-container id, and sets `refcount = 1`; later workers
     read the URL and increment.
   - On teardown each worker decrements; the worker that brings `refcount` to `0`
     stops and removes the container **by id** (any worker can, via the recorded
     id) and deletes the state file.
   - Ryuk is disabled for the run (`testcontainers` `ryuk_disabled`) because the
     refcount now owns the container lifecycle; a container shared across processes
     cannot be tied to any one creator's exit.

3. **Isolation moves to database/bucket per worker.** Each worker provisions
   `kdive_test_<worker>` (worker id from `PYTEST_XDIST_WORKER`, `master` when not
   under xdist) with `DROP DATABASE IF EXISTS … (FORCE); CREATE DATABASE …` on the
   shared server and yields that database's conninfo; `minio_store` provisions and
   empties a `kdive-test-<worker>` bucket. Setup is idempotent so a database/bucket
   left by a crashed prior run against a *persistent* (override) backend is
   reclaimed, not a hard error. Per-worker teardown drops its own database/bucket.
   `pg_conn`'s per-test `DROP SCHEMA public` and `key_ns`'s per-test prefix are
   unchanged and now operate inside the per-worker namespace.

4. **`KDIVE_REQUIRE_DOCKER=1` is preserved verbatim.** The import-guard and
   container-start failure paths still skip when unset and re-raise when set, on both
   the override-absent path (start) and unchanged otherwise.

The flock/refcount/per-worker-naming coordination is factored into one small test
support helper so the Postgres and MinIO fixtures share exactly one implementation of
the algorithm rather than duplicating it.

## Consequences

- One Postgres and one MinIO container per `just test` run instead of ~18 each;
  start cost paid once, no serial `container.stop()` tail. Recovers the ~15–25s
  targeted by #1331.
- The fixtures gain cross-process coordination code (flock + refcount + stop-by-id).
  This is the cost of a shared mutable resource across xdist processes; it is
  contained in one helper and exercised on every `-n auto` run.
- **Residual — leaked container on hard worker crash.** With Ryuk disabled, a worker
  killed (SIGKILL/OOM) before teardown never decrements, so `refcount` never reaches
  `0` and the shared container survives the run. This is bounded (one container, not
  18), self-healing on the next run against a container path (a fresh container is
  started; the stale one is orphaned, not reused), and cleaned by
  `docker container prune` or CI runner recycling. Documented in the fixture
  docstrings.
- **Residual — override backend requires `CREATEDB`.** The env-override server must
  let its user create/drop databases and buckets. The `just compose-up` `kdive`
  superuser and `minioadmin` root satisfy this; a locked-down shared backend would
  fail loudly at `CREATE DATABASE`, which is the correct signal.
- POSIX-only coordination (`fcntl.flock`). The suite already targets Linux/macOS
  developer and CI hosts; Windows is out of scope for the live/db suites.
- Pairs with, but does not depend on, `--dist worksteal` (#1332) and truncate-based
  schema reset (#1333, which depends on this).

## Considered & rejected

- **Do nothing.** Rejected: the churn is a measured ~25–40% of `just test` wall time
  and grows with core count; the isolation it buys is recoverable more cheaply with
  per-worker databases.
- **Env override only (the issue's literal "preferred").** Reuse a backend when
  `KDIVE_TEST_*` is set, else keep one container per worker. Rejected as the sole
  mechanism: a bare `just test` and CI (which set no override) would keep starting
  one container per worker, so the headline speedup would not land by default. Kept
  as the *first* branch, layered over self-coordination.
- **Eager start via xdist controller hooks** (`pytest_configure` +
  `pytest_configure_node` injecting the URL into `workerinput`,
  `pytest_sessionfinish` stopping it). Cleaner coordination — the controller
  outlives all workers, so no flock, no refcount, no Ryuk hazard. Rejected because it
  is **eager**: the controller would start Postgres and MinIO on every xdist run,
  including DB-less subsets and the inner-loop recipes (#1334) that touch neither.
  Laziness is worth the coordination cost.
- **testcontainers `reuse` / `TESTCONTAINERS_REUSE_ENABLE`.** Leaves the container
  running between runs, keyed by a config hash. Rejected: it leaks a long-lived
  container by design (no deterministic teardown), muddies the
  `KDIVE_REQUIRE_DOCKER` contract, and risks cross-run state bleed if the reset
  contract ever regresses.
- **`filelock` dependency instead of stdlib `fcntl.flock`.** The canonical xdist
  recipe uses the `filelock` package. Rejected: it is a new runtime/dev dependency
  for a lock the stdlib already provides on every platform the suite targets.
- **Single owner worker (gw0 starts/stops).** Rejected: gw0 can finish before other
  workers, stopping the shared container mid-run. Refcount teardown is required for
  correctness precisely because worker completion order is unspecified.
