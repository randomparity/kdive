# Systemd proof case isolation

Issue #2566.

## Problem

The real-systemd proof now accepts systemd's per-template slice, and its first two cases pass on
the hosted runner. The database-outage case still assumes that a unit's `cgroup.events` file
survives until it can observe `populated 0`. On the exercised systemd version, the cgroup directory
is removed immediately after the last process exits. `_wait_for_empty_cgroup` raises
`FileNotFoundError` instead of accepting the stronger evidence that no process remains.

The outage case restores PostgreSQL, but its inline worker cleanup then fails. Because cleanup is
owned by individual test bodies, the next three cases inherit the residual fleet and fail their
initial `start` with a conflict. The exact-head live result is therefore two passed cases, one real
failure, and three consequential failures.

## Scope

Move cgroup-population observation into the non-collected proof support module. A blank systemd
`ControlGroup`, or disappearance of the exact `cgroup.events` path while reading it, means
unpopulated. Every nonempty record in a present file must contain exactly a key and value, and the
`populated` record must occur exactly once with value `0` or `1`. Missing, duplicate, malformed,
undecodable, or otherwise unreadable present evidence remains a failure.

Add an autouse, function-scoped fixture to the live proof. After every case that reached the
module's validated `proof_context`, it:

1. restores and health-checks the exact Compose PostgreSQL container if needed;
2. stops and recovers the fixed worker fleet.

Database restoration precedes lifecycle cleanup because lifecycle state transitions require the
database. A restoration failure prevents a misleading cleanup attempt and is reported. A cleanup
failure is reported as teardown failure. Pytest preserves a test-body failure independently from a
teardown failure, so cleanup cannot turn red proof evidence green or replace its primary traceback.
Where the outage case performs recovery inside its body, a secondary restoration or cleanup
exception is attached to the primary with its message and formatted traceback rather than only its
exception class.
The outage case keeps its narrower recovery block because it must inspect retained database state
before returning; the fixture is the fail-safe boundary around that block.

The basic worker cases keep `_assert_stopped` on their successful path: terminal database rows,
empty lifecycle status, inactive identity-free units, and removed slot artifacts are proof
assertions, not merely cleanup. Their fixture is the failure-safe fallback if startup or any later
assertion aborts. Remove only redundant worker-only `try/finally` blocks from the three recover
cases. Keep the partial-start helper's local `finally`, which also owns a temporary systemd
drop-in, and keep the outage case's ordered restoration/evidence logic.

No production lifecycle, unit, database, workflow, protocol, or persisted-state contract changes.
The hosted workflow invocation remains owned by #2565; residual production recovery remains owned
by #2533 and #2596.

## Failure model

- Fixture setup failure starts no case and therefore creates no new worker cleanup obligation.
- PostgreSQL restoration failure is reported and gates database-dependent fleet cleanup.
- Fleet cleanup failure is reported without suppressing an existing test failure.
- A vanished cgroup path is accepted only as process-absence evidence; unit identity and retained
  lifecycle facts continue to come from systemd properties and PostgreSQL.
- An unexpected permission, decoding, or malformed-content error remains visible.

## Success

1. Focused tests prove populated, empty, blank, disappeared, and malformed cgroup observations.
2. Recovery-helper tests preserve a primary error and record the secondary message and traceback.
3. Isolated pytest subprocesses prove teardown runs after startup and post-start assertion failures,
   and prove a body failure plus teardown failure are both reported.
4. The basic cases still assert full terminal evidence on their successful path.
5. The exact six-case hosted proof passes in one run, including the outage and three recovery cases.
6. Focused guardrails and `just ci` pass without production or workflow changes.

## Validation

- **Support behavior — Mode: focused-test.** Run `just test-verbose
  tests/live_vm/test_systemd_worker_lifecycle_support.py`. The disappeared-path case raises before
  the implementation and returns false afterward. Malformed, missing, duplicate, undecodable, and
  non-not-found I/O evidence stays red. Recovery tests inspect secondary traceback text.
- **Fixture isolation — Mode: focused-test.** In the same support file, use pytest's subprocess
  tester with the real live-proof module registered as a plugin. Override `proof_context` with a
  fake carrying a sentinel container ID and patch restoration/reset boundaries for observation.
  Inject failures before and after a start marker and assert the live autouse fixture calls both
  boundaries with the sentinel; then inject body and teardown failures together and assert both
  reports remain visible. A structural assertion pins autouse function scope, the
  `proof_context` dependency, and the call to the shared cleanup helper.
- **Proof structure — Mode: focused-test.** Collect
  `tests/live_vm/test_systemd_worker_lifecycle.py` and require exactly the same six node IDs. A
  source guard pins `_assert_stopped` on the basic cases' normal path so cleanup isolation cannot
  weaken their termination proof.
- **Hosted systemd proof — Mode: live-test.** Dispatch the exact branch head through `live.yml` and
  require all six `tests/live_vm/test_systemd_worker_lifecycle.py` cases to pass. The workflow's
  existing cleanup step remains the outer fail-safe.
