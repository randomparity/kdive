# Systemd proof case isolation — implementation plan

**Goal:** Make all six real-systemd cases independent and accept normal cgroup removal after worker
exit.

**Architecture:** Keep all changes in the live-proof harness. The support module owns cgroup reads
and ordered recovery; a function-scoped pytest fixture owns per-case restoration and fleet reset.

**Tech stack:** Python 3.14, pytest, systemd, Docker Compose, `uv`, `just`.

Measured implementation size: 238 changed lines against the frozen 100-line denominator (S). This
is a non-blocking 238% expansion warning: strict malformed-cgroup coverage and subprocess proof of
the real fixture's body/teardown behavior account for the increase. The reviewed three-file test
surface and production exclusions are unchanged.

## Constraints

- Base branch: `main`; branch: `feat/systemd-proof-cleanup-2566`; sibling worktree only.
- Preserve the six-case roster and the corrected per-template cgroup expectation.
- Change only the three live-proof files and these design artifacts.
- Do not change production lifecycle behavior, hosted workflow invocation, provisioning, or
  residual-worker recovery.
- Public artifacts contain no private host, user, network, or location identifiers.

## Task 1: Make cgroup absence explicit proof behavior

**Files:** modify `tests/live_vm/systemd_worker_lifecycle_support.py` and
`tests/live_vm/test_systemd_worker_lifecycle_support.py`.

**Interface:** add `cgroup_populated(control_group: str, *, root: Path = Path("/sys/fs/cgroup"))
-> bool` to the proof support module. Blank control groups and a missing exact events file return
false. Present files require exactly one `populated` record with value `0` or `1`; every malformed
record, decoding error, and non-not-found I/O error propagates.

**Steps**

1. Add focused tests using a temporary cgroup root for `populated 1`, `populated 0`, blank control
   group, and a path removed before the read. Add rejected cases for a missing or duplicate
   `populated` record, malformed record/value, invalid UTF-8, and `PermissionError`. Confirm the
   removed-path test fails against the old local helper behavior.
2. Implement the minimum support helper and switch `_unit_evidence` and
   `_wait_for_empty_cgroup` to it.
3. Run `just test-verbose tests/live_vm/test_systemd_worker_lifecycle_support.py`, `just lint`, and
   `just type`.

Acceptance: systemd cgroup removal is equivalent to `populated 0`; malformed present evidence is
not softened.

Rollback: revert the task commit; no external state changes.

## Task 2: Put worker cleanup at the fixture boundary

**Files:** modify `tests/live_vm/test_systemd_worker_lifecycle.py` and
`tests/live_vm/test_systemd_worker_lifecycle_support.py`.

**Interface:** add `cleanup_after_case(*, restore_database, cleanup_workers) -> None` to the support
module and an autouse function-scoped fixture depending on `proof_context`. The fixture calls that
helper to restore the exact proof database and then `_reset_fleet`.

**Steps**

1. Extend `_retain_primary_failure` so its note includes the secondary exception's message and
   formatted traceback while the original exception remains primary. Add `cleanup_after_case` as
   the fixture-facing wrapper around ordered recovery.
2. Add the fixture after lifecycle helpers are defined. Its teardown calls `cleanup_after_case`;
   pytest reports any raised cleanup error separately from a body failure.
3. Keep `_assert_stopped(proof_context, rows)` on the basic cases' successful body path and retain
   the three recover cases' existing `try/finally` cleanup. The fixture covers failures before or
   inside those blocks. Keep the outage recovery block and partial-start drop-in cleanup.
4. Extend support tests to assert restoration precedes cleanup, restoration failure gates cleanup,
   cleanup failure is raised, and an existing primary failure note carries the secondary message
   and traceback.
5. Use `pytester` subprocess runs with the real live-proof module registered as a plugin. Supply a
   nearer `proof_context` fixture carrying a sentinel container ID and patch
   `support.restore_postgres` plus `_reset_fleet` from that fixture so the patches outlive the
   autouse teardown. Inject startup and post-start assertion failures and verify both boundaries
   run after each. In a separate run, make reset fail and assert pytest reports both the body and
   teardown messages. Add an AST/source assertion pinning the real fixture's autouse function
   scope, `proof_context` dependency, exact-container restoration, and `cleanup_after_case` wiring.
6. Collect the live file and assert the six node IDs are unchanged. Add a narrow source guard that
   the basic cases still call `_assert_stopped` on the normal path. Run `just test-changed`, `just
   lint`, and `just type`.

Acceptance: an exception anywhere after a case starts workers still reaches ordered fixture
cleanup, cleanup failures remain visible alongside the primary failure, and the normal success
path still proves terminal rows, empty status, inactive units, and absent slot artifacts.

Rollback: revert the task commit; the workflow's outer cleanup remains available.

## Task 3: Verify and ship

1. Stage the exact five changed paths, run `prek run`, re-add only rewritten staged paths, and
   commit with Conventional Commit subjects.
2. Run `just ci > /tmp/kdive-2566-ci.log 2>&1 < /dev/null` and preserve its exit status.
3. Push the exact reviewed head, open the issue-linked pull request, and wait for all required
   checks.
4. Dispatch `live.yml` at that exact head. Require the systemd proof to report six passed cases and
   its outer cleanup step to succeed. Treat later TCG-tier behavior as separate evidence.
5. Merge with history only after the branch is current, checks are green, review is approved, and
   exact-head live evidence is green; then clean the branch and worktree.
