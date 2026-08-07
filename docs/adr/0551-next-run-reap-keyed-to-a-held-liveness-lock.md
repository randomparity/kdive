# 0551 — A crash-stranded backend container is reaped by the next run, keyed to a held liveness lock

## Status

Accepted (2026-08-06)

## Context

[ADR-0401](0401-one-backend-container-per-run.md) collapsed ~18 per-worker Postgres and
MinIO containers into one shared container per run, coordinated across xdist worker
processes by an `fcntl.flock` guard and a refcounted JSON state file
(`tests/support/xdist_backend.py`). It disabled testcontainers' Ryuk reaper as part of
that decision, because Ryuk's reap is keyed to the process that created a container and
this container is deliberately shared across processes.

That left the refcount as the only reaper, and ADR-0401 recorded the resulting leak as a
bounded, self-healing residual: "a hard crash can leak **up to two** backend containers
… still bounded (two, not ~36), self-healing on the next run against a container path (a
fresh container is started; the stale one is orphaned, not reused)".

**Field evidence falsifies that characterization** (#1910, dev host, 2026-08-06):

```
$ docker ps -a --filter "label=org.testcontainers=true" --format '{{.Names}} {{.Status}}'
21 containers
romantic_mendel     Up 2 days
trusting_margulis   Up 4 days
...
laughing_payne      Up 10 days
```

The orphans run `postgres -c max_connections=500`, which is `_start_postgres` plus
`xdist_backend.postgres_max_connections()` verbatim — unambiguously this repo's fixtures.
Accrual was roughly two per day. They pinned ~7.5 GB of anonymous volumes that
`docker volume prune` cannot reclaim, because a volume attached to a live container is
in use and prune skips it.

The reasoning error is in the word *orphaned*. "Orphaned, not reused" is a correctness
claim — the next run does not read stale state — and it is true. It was written as though
it were also a lifecycle claim, and it is not: nothing in the system ever removes the
orphan. The bound "two containers" is per crashed run, not per host, so the aggregate is
unbounded in exactly the way the ADR's own *persistent-override* residual already
acknowledged for leftover `kdive_test_*` databases. The container path was assumed to
need no sweep because "the container is gone" — which holds only when teardown ran.

The forces on a remedy:

- **The four failure modes differ in how much unwinding they allow.** Ctrl-C and a
  cancelled run let Python finalizers run; SIGKILL and OOM do not. A remedy that only
  runs in-process covers half the reported modes.
- **Ryuk's process assumption is unchanged.** In testcontainers-python 4.14.2,
  `testcontainers/core/labels.py` binds `SESSION_ID` to a module-level `uuid4()` — one
  value per *process* — and `Reaper` is a process-global singleton holding a single
  socket, which sends `label=org.testcontainers.session-id=<SESSION_ID>` and reaps on
  disconnect. Under xdist each worker has its own `SESSION_ID`; the shared container is
  stamped with the creating worker's. The first worker to exit therefore reaps the
  container out from under every worker still using it. This is ADR-0401's stated hazard,
  re-verified against the pinned version rather than taken on trust.
- **This host runs concurrent suites.** Any next-run sweep needs a staleness predicate
  that cannot reap a container belonging to a suite running right now.
- **A staleness predicate must not be a timer.** Age is not liveness: a long
  `live_stack` run and a crashed run five minutes ago are indistinguishable by clock.
- **The clean path is already fixed.** #1912 and #1914 added `v=True` to the refcount
  teardown so the anonymous volume goes with the container. That covers orderly
  teardown only; the crash path removes nothing at all.

## Decision

**Keep Ryuk disabled. Add a next-run sweep whose staleness predicate is a `flock` that
the owning run holds for the whole life of its reference.** The refcount keeps owning the
orderly path; the sweep is the crash path, and the two never disagree because a run that
tore down cleanly has already removed its container.

1. **Every holder holds a shared lock for its reference's lifetime.** `shared_container`
   wraps its whole body in a `LOCK_SH` on a per-run, per-backend file
   `<run-root>/kdive-<name>.alive`, opened once and held open. `LOCK_SH` is the right
   mode because every xdist worker holding a reference takes one concurrently; they
   coexist. The lock is taken *before* the container is created, so a container that
   exists always has a held lock behind it.

2. **Every backend container carries two labels** naming what it is and where its
   liveness lock lives:

   - `kdive.test-backend` — the backend name (`pg`, `minio`)
   - `kdive.test-backend-liveness` — the absolute path of that run's `.alive` file

   `shared_container` computes them and passes them to the injected `start` callable,
   which applies them via testcontainers' `with_kwargs(labels=…)`. The
   `org.testcontainers` namespace is reserved by `create_labels`, so a private `kdive.`
   namespace is required rather than preferred.

3. **A run sweeps before it starts a container.** `_start_postgres` / `_start_minio` call
   `sweep_stale_backend_containers()` first. It lists containers filtered by
   `kdive.test-backend`, and for each opens the recorded liveness path and attempts
   `LOCK_EX | LOCK_NB` on it:

   - **acquired** — no process holds a shared lock, so the owning run is gone by any
     route including SIGKILL, because the kernel releases `flock` on process death
     whatever killed it. Remove the container with `force=True, v=True`, taking the
     anonymous volume with it.
   - **`EWOULDBLOCK`** — a live run holds it. Leave it alone.
   - **file missing** — the run root was removed, which pytest does only to roots of
     finished runs. Treat as gone.
   - **anything else** — unreadable, or not a regular file. Treat as **live**.
   - **no `kdive.test-backend-liveness` label** — not provably ours. Never reap.

   **Only a missing file answers "dead".** The asymmetry is deliberate and is the safety
   property: failing to reap costs one stale container until the next run, while reaping
   wrongly destroys a running suite's backend mid-test. The multi-user case makes this
   concrete — pytest's per-run root is `/tmp/pytest-of-<user>`, mode 0700, so on a shared
   Docker daemon every container another user's *live* run owns has a liveness path this
   process cannot stat. A `Path.is_file()` guard reports False for exactly that and would
   read it as reapable.

   For the same reason the predicate is **one open, not a stat-then-open**: the path comes
   off a container label, so it is neither necessarily ours nor stable across two
   resolutions, and checking one resolution while opening another leaves a symlink-swap
   window. `O_RDONLY | O_NONBLOCK` keeps the open from blocking on a FIFO — which would
   hang the sweep and the run behind it, with no timeout to catch it — and `fstat` on the
   returned descriptor interrogates the object actually opened.

   The sweep is best-effort: a Docker failure warns and the run continues. A concurrent
   sweep from another run racing to remove the same container gets `NotFound`, which is
   suppressed rather than warned, because it means the work was done.

The predicate is a direct, kernel-enforced liveness signal rather than an inference from
age, name, or pid, so it cannot reap a running suite's container and cannot fail to reap
a killed one. `fcntl.flock` is already this helper's coordination primitive, so this adds
a second use of an existing mechanism rather than a new one.

## Consequences

- A crash-stranded Postgres or MinIO container survives at most until the next run that
  starts a backend container on the same host, instead of forever. The 21-container,
  ~7.5 GB accrual this ADR was written against cannot recur.
- The anonymous volume is reclaimed on the crash path too, closing the gap #1912/#1914
  left open. `docker volume prune` remains unable to help, and no longer needs to.
- One additional Docker API call (`containers.list` with a label filter) per container
  start, and one open file descriptor per worker holding a backend reference.
- `shared_container`'s `start` callable takes the label mapping as an argument. The
  helper stays free of any Docker import; only the two conftest start functions and
  `sweep_stale_backend_containers` know about Docker.
- **Residual — a crashed run's container survives until the next run.** Nothing reaps at
  crash time. On a host where the suite is never run again, the container stays. This is
  the deliberate trade for a predicate that is safe under concurrent suites; the
  alternative that reaps at crash time is Ryuk, and it is unsafe here for the reason
  above.
- **Residual — containers created before this change are never swept.** They carry no
  `kdive.test-backend` label, and the sweep refuses to reap what it cannot prove it owns.
  Clearing the existing backlog is a one-time operator command, recorded under
  `AGENTS.md` "Host prerequisites" beside the Docker requirement it belongs to.
- **Residual — a run root removed while its run is live.** The liveness file lives under
  pytest's per-run temp root. pytest keeps the three most recent roots and honors its own
  lock file before removing any, so removing a live run's root requires a run older than
  pytest's lock timeout with three newer runs behind it. If it did happen, the sweep
  would read the absent file as "gone" and reap a live container, and the affected run
  would fail loudly on a dropped connection rather than corrupt anything.
- **Residual — Ryuk stays disabled process-wide.** ADR-0401's consequence is unchanged
  and now has a partner: a future throwaway testcontainer added to the suite still owns
  its own teardown, and it gets crash coverage only by carrying these two labels.
- The reap is a destructive external write. It is bounded to containers carrying this
  repo's own label whose recorded lock is provably unheld; nothing else on the host is
  enumerated, matched, or removed.
- POSIX-only, same as ADR-0401's coordination. `LOCK_SH` / `LOCK_EX | LOCK_NB` semantics
  and kernel release on SIGKILL are pinned by a test that kills a real child process.

## Considered & rejected

- **Re-enable Ryuk** (the issue's first suggestion). Rejected: re-verified against
  testcontainers 4.14.2 rather than assumed. `SESSION_ID` is per-process and `Reaper` is
  a per-process singleton, so the shared container is stamped with the first worker's
  session and reaped ~10s after that worker exits, mid-run, for every other worker. Making
  it work would mean forcing a run-shared `SESSION_ID` into
  `testcontainers.core.labels` in every worker *and* holding a Ryuk socket in a process
  that outlives them all — reaching into a dev dependency's private module state, where a
  version bump breaks it silently and the failure mode is the leak returning unnoticed.
- **`pytest_sessionfinish` or `atexit` only** (the issue's second suggestion). Rejected as
  the sole mechanism: neither runs on SIGKILL or OOM, two of the four modes #1910 names.
  It also adds little on the modes it does cover — pytest already tears down session
  fixtures on Ctrl-C via `_pytest.runner.pytest_sessionfinish` calling
  `teardown_exact`, which is the refcount path that already works. A backstop for an
  already-covered path, blind to the paths that actually leak.
- **Age-based staleness** (reap any labeled container older than N hours). Rejected: age
  is not liveness. On a host running concurrent suites, the only N that never reaps a
  live run is one long enough to leave the leak in place for that long.
- **Liveness by recorded pid.** Store each holder's pid in the state file and reap when
  all are dead. Rejected: pid reuse makes a dead run look live, and the check has to read
  another run's state file to work at all. `flock` answers the same question with a
  kernel guarantee and no reuse hazard.
- **Reap on the run root's absence alone** (let pytest's `keep=3` rotation be the
  signal). Rejected as the sole predicate: it works, but leaves a stranded container for
  three further runs and binds correctness to pytest's rotation policy, which is not a
  contract this repo owns. Kept only as the secondary case for a root that is already
  gone.
- **`TESTCONTAINERS_REUSE_ENABLE`.** Rejected for the reasons ADR-0401 gave and one more:
  it makes the long-lived container the intended state, so no sweep could distinguish a
  leak from a reuse.
- **A sweep wired as a `pytest_configure` hook or a `just` recipe.** Rejected: it would
  run on every invocation including the DB-less subsets ADR-0401 kept cheap, and a recipe
  would not cover a direct `pytest -n auto tests/db`. Sweeping inside the container-start
  path costs nothing when no container is started and covers every entry point.
