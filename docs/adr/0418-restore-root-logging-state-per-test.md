# ADR 0418 — Restore root-logger state around every test to contain entrypoint-bootstrap leaks

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-21
- **Deciders:** D. Christensen (core platform)

## Context

PR#1394 (#1332) switched the `just test` recipe's xdist distribution from the default
`--dist load` to `--dist worksteal` to shorten the straggler tail. worksteal adds no
concurrency — a worker still runs its assigned tests serially — but it changes *which*
tests land on the same worker and in what order. That order change surfaced a latent test
that leaks process-global logging state, which then breaks two otherwise-unrelated
log-assertion tests: `test_wait_job_degrades_invariant_violating_terminal_row` and
`test_list_jobs_isolates_invariant_violating_row` (`tests/mcp/jobs/test_jobs_tools.py`).
Both force a producer-bug row (`state='failed', error_category=NULL`), call the tool, and
assert that a `kdive.mcp.tools.jobs` degrade WARNING with `exc_info` was captured by
`caplog`. Under worksteal in CI, `caplog.records` was empty for that WARNING even though the
record was clearly emitted (it showed in the captured stderr and the `--- Logging error ---`
report), so the `any(...)` assertion evaluated `False`.

Root cause: `main()` (`src/kdive/__main__.py`) — and every real entrypoint — calls
`bootstrap_stdout_floor`, which attaches a `_KdiveHandler` (a `logging.StreamHandler`
subclass, `src/kdive/log.py`) to the **root** logger bound to the live `sys.stderr`. Under
pytest, `sys.stderr` at that moment is the per-test capture buffer, which pytest closes when
the test ends. Several tests in `tests/test_main_version.py` exercise `main([...])` for real
(e.g. the `test_categorized_error_*` cases call `main(["build-fs", ...])`, and
`test_startup_logs_version` calls `main(["reconciler"])`) without stubbing the bootstrap, so
each leaves that root handler pointed at a now-closed stream. `tests/config/test_entrypoint_validation.py`
already documents the hazard in a comment and stubs `bootstrap_stdout_floor`/`init_telemetry`
by hand — but that opt-in discipline is per-test and easy to miss.

On the next test that shares the worker, any record propagating to root makes the stale
handler's `emit` call `stream.write` on the closed stream and raise
`ValueError: I/O operation on closed file` mid-`Logger.callHandlers`. That disrupts the
record's delivery to pytest's `caplog` handler, silently dropping it from
`caplog.records` — which is exactly the invisible, order-dependent failure the two jobs tests
hit. Under `--dist load` the leaking `test_main_version.py` tests and the jobs tests happened
not to share a worker in the observed run; worksteal reshuffled them onto the same worker.

The failure reproduces deterministically single-process:
`pytest tests/test_main_version.py tests/mcp/jobs/test_jobs_tools.py::test_wait_job_degrades_invariant_violating_terminal_row tests/mcp/jobs/test_jobs_tools.py::test_list_jobs_isolates_invariant_violating_row -n0`
fails both jobs tests; dropping `test_main_version.py` from the argument list passes.

This is a test-isolation defect, not a product defect. In production `main()` runs the
bootstrap once and keeps the handler for the process lifetime, which is correct; the stream
is only ever closed underneath the handler because pytest owns and recycles `sys.stderr`.

## Decision

Add an autouse `restore_root_logging` fixture in the top-level `tests/conftest.py` that
snapshots the mutable global root-logger state before each test and restores it after:

- the root logger's handler list (`root.handlers`),
- the root logger's level (`root.level`),
- the global `logging.disable` floor (`logging.root.manager.disable`).

Restoring the handler list after every test removes any handler a test attached to root —
including the entrypoint bootstrap's `_KdiveHandler` bound to a soon-to-be-closed capture
stream — so no test ordering can carry one test's logging mutation into another. The two
other pieces (level and the `disable` floor) are the other process-global logging knobs a
test can move that would suppress a later test's `caplog` capture; restoring them closes the
same isolation gap for those knobs at negligible cost.

The fixture wraps the whole test, so pytest's own per-phase `caplog`/report handler
management (which adds and removes its handlers *within* the call phase) nests inside it and
is unaffected; the pre-test snapshot preserves any session-level handlers pytest installs
before the first test, since they are already present when the snapshot is taken.

## Consequences

- The two jobs degrade tests pass regardless of what else shares their worker; the
  `--dist worksteal` change in PR#1394 stays.
- Any current or future test that mutates root handlers, the root level, or the `logging.disable`
  floor is contained automatically — the recurring "did you remember to stub the bootstrap"
  footgun that `tests/config/test_entrypoint_validation.py` guards against by hand is now
  covered structurally for the root-handler case. The manual stubs there are left in place
  (they also assert bootstrap ordering/arguments, which the fixture does not).
- No production code, schema, or migration changed. Only `tests/conftest.py` gained the
  fixture; the `justfile` worksteal change is unrelated and retained.

## Considered & rejected

- **Fix only the leaking `tests/test_main_version.py` tests** (stub the bootstrap, or wrap
  each `main([...])` call to restore root handlers). Rejected: multiple tests in that file
  leak, the same footgun already recurs in `tests/config/test_entrypoint_validation.py`, and
  any future test that drives a real entrypoint would reintroduce the flake. A per-test cure
  does not generalize; the isolation boundary belongs in one autouse fixture.
- **Change the product so `bootstrap_stdout_floor` binds a copy of the stream or re-resolves
  `sys.stderr` lazily.** Rejected: the production behavior is correct (bind the real
  `sys.stderr` once and keep the handler). The defect is entirely that pytest recycles the
  captured stream underneath a leaked handler — a test-lifecycle concern that must not
  distort the production logging contract.
- **Snapshot and restore every logger's `propagate` and `level` in the fixture too.**
  Considered as maximal robustness, rejected as premature: the observed and reproduced leak
  is a leaked root *handler*, and no test in the suite leaves a `kdive`-ancestor `propagate`
  false or a stray `disable` floor (verified with a teardown-time detector run across the
  full suite). Restoring root handlers, root level, and the `disable` floor covers the
  demonstrated failure class without iterating the whole logger hierarchy on every one of
  ~9000 tests. If a future leak of ancestor `propagate` appears, the fixture is the place to
  extend.
