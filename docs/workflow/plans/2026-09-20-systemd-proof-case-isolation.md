# Combined live-proof gate recovery — implementation plan

**Goal:** Make all six real-systemd cases independent and produce one exact-head live result that
also exercises the corrected console-artifact request and marker-bearing part selection.

**Architecture:** Keep all changes in test and live-proof support. The systemd support module owns
cgroup reads and ordered recovery; a function-scoped pytest fixture owns per-case restoration and
fleet reset. Shared live-stack support owns full artifact paging, while non-collected console-part
support owns marker-aware polling over immutable part artifacts.

**Tech stack:** Python 3.14, pytest, systemd, Docker Compose, `uv`, `just`.

Expected implementation size: 500–650 changed lines (M) — the existing 517-line #2566 proof
harness plus #2608 paging coverage and marker-aware polling support.

## Constraints

- Base branch: `main`; branch: `feat/systemd-proof-cleanup-2566`; sibling worktree only.
- Preserve the six-case roster and the corrected per-template cgroup expectation.
- Change only the three systemd live-proof files, the console live proof, shared live-stack test
  support and its focused tests, and these design artifacts.
- Do not change production lifecycle behavior, hosted workflow invocation, provisioning, or
  residual-worker recovery, artifact API behavior, or console capture and rotation.
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
7. From the first isolated live result, pin the three newly reachable proof mechanics: expected
   dependency failures use `_lifecycle_result` and exit status 4; out-of-band restart setup polls
   for the terminal failed state for at most ten monotonic seconds; outage cleanup proves `stop`
   retirement, accepts the blank `ControlGroup` after cgroup removal, then calls `recover` to clear
   the failed unit identity. Add source guards for those contracts and rerun the focused support
   file, lint, and type checks.

Acceptance: an exception anywhere after a case starts workers still reaches ordered fixture
cleanup, cleanup failures remain visible alongside the primary failure, and the normal success
path still proves terminal rows, empty status, inactive units, and absent slot artifacts.

Rollback: revert the task commit; the workflow's outer cleanup remains available.

## Task 3: Integrate artifact paging and marker-aware polling

**Files:** merge the two #2608 commits, modify
`tests/integration/test_console_parts_live.py`, create
`tests/integration/live_stack/console_parts.py` and
`tests/integration/live_stack/test_console_parts.py`, and retain the #2608 changes in
`tests/integration/live_stack/spine.py` and `tests/integration/live_stack/test_spine.py`.

**Interfaces:** retain `full_artifact_text(client, artifact_id, phase_name) -> str`. Add
`poll_for_new_console_part(client, system_id, initial_ids, marker, *, deadline_s,
interval_s) -> tuple[str, str]`. The returned pair is the marker-bearing artifact id and its full
plaintext. `test_console_parts_live.py` supplies the existing 300-second deadline and five-second
interval and no longer fetches the selected part a second time.

The support loop has this exact state transition: list current parts, discard initial and already
inspected ids, fetch each remaining immutable part with `full_artifact_text`, add it to the
inspected set, and return `(artifact_id, text)` only when `marker in text`. If none matches, compare
`time.monotonic()` with the fixed deadline, raise `SpinePhaseError` after expiry, or await
`asyncio.sleep(interval_s)` before relisting. Listing, paging, and read failures propagate.

**Verification**

- **Nested artifact request — Mode: focused-test.** Test
  `tests/integration/live_stack/test_spine.py::test_full_artifact_text_nests_request_and_pages`.
  Expected red: reverting the #2608 implementation records flat artifact arguments. Green command:
  `uv run python -m pytest
  tests/integration/live_stack/test_spine.py::test_full_artifact_text_nests_request_and_pages -q`.
- **Marker-aware polling — Mode: focused-test.** Test
  `tests/integration/live_stack/test_console_parts.py`. Expected red: the support function is
  absent before this task. Green command: `uv run python -m pytest
  tests/integration/live_stack/test_console_parts.py -q`.

**Steps**

1. Merge the reviewed #2608 branch into this branch without rewriting its two commits. Confirm the
   paging test passes and the combined diff contains no production files.
2. Add a scripted fake-client test whose first listing introduces one immutable part without the
   marker and whose second introduces a marker-bearing part. Assert the first part is fetched once,
   the later id and full text are returned, and artifact reads retain nested request paging.
3. Implement the non-collected support function. Track inspected artifact ids, scan every unseen
   post-snapshot candidate, and sleep only when no candidate contains the marker. On deadline,
   raise `SpinePhaseError` with initial, current, and inspected counts.
4. Replace the live module's local listing and polling helpers with the support call and consume
   its returned plaintext directly.
5. Run both focused commands, `just test-changed`, `just lint`, and `just type`.

Acceptance: immutable parts lacking the marker cannot cause a false failure, no part is fetched
twice while polling, tied timestamps do not control selection, and the production capture and API
surfaces remain unchanged.

Rollback: revert the marker-polling commit and the merge commit together; neither changes external
state.

## Task 4: Verify and ship

1. Stage the exact changed paths, run `prek run`, re-add only rewritten staged paths, and
   commit with Conventional Commit subjects.
2. Run `just ci > /tmp/kdive-2566-ci.log 2>&1 < /dev/null` and preserve its exit status.
3. Push the exact reviewed head, open the issue-linked pull request, and wait for all required
   checks.
4. Dispatch `live.yml` at that exact head. Require the systemd proof to report six passed cases,
   the native console-parts proof to pass, both jobs to complete successfully, and cleanup steps to
   succeed.
5. Merge with history only after the branch is current, checks are green, review is approved, and
   exact-head live evidence is green; then close PR #2609 as superseded and clean both owned
   branches and worktrees.
