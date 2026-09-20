# Combined live-proof gate recovery

Issues #2566 and #2608.

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

The first exact-head verification after isolating cases exposed three more proof-harness
assumptions that earlier cascades had hidden. Expected `status` and `stop` dependency failures
used a success-only subprocess wrapper, an out-of-band restart sampled a `Type=simple` unit before
its gate process reached the terminal failed state, and outage cleanup expected `stop` to clear a
failed unit identity even though the lifecycle contract assigns that residual cleanup to
`recover`. The proof must decode expected nonzero responses, wait boundedly for the terminal
systemd state, accept the blank `ControlGroup` that follows cgroup removal, and prove stop
retirement before invoking recovery to clear the identity.

After those corrections passed all six systemd cases, the same exact-head workflow reached a
separate native console-parts proof and failed before its assertions because that test still sent
the retired flat `artifacts.get` arguments. Correcting the request exposed a synchronization
defect: the poll returns as soon as any immutable part appears after the initial snapshot, even
when that part was sealed by an already-running rotation before the marker reached the console.
Retained proof artifacts showed the chosen part lacked the marker while later parts from the same
System contained it. Because every dispatched SHA runs both live jobs, the two issue branches must
be integrated to produce one mergeable exact-head result.

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

Expected lifecycle failures use the existing nonchecking response decoder and require exit status
4. Out-of-band restart setup polls for `ActiveState=failed` for at most ten monotonic seconds,
avoiding the transient `active/running` state systemd may expose immediately after a `Type=simple`
start. After the outage database is restored, cleanup first proves `stop` retired the row and
removed slot artifacts, then calls `recover` and requires an inactive unit with empty identity.

The basic worker cases keep `_assert_stopped` on their successful path: terminal database rows,
empty lifecycle status, inactive identity-free units, and removed slot artifacts are proof
assertions, not merely cleanup. Their fixture is the failure-safe fallback if startup or any later
assertion aborts. Keep the existing inline cleanup in the three recover cases as their normal path;
the fixture covers failures in `_assert_started` before those blocks begin and failures inside the
blocks' cleanup. Keep the partial-start helper's local `finally`, which also owns a temporary
systemd drop-in, and keep the outage case's ordered restoration/evidence logic.

No production lifecycle, unit, database, workflow, protocol, or persisted-state contract changes.
The hosted workflow invocation remains owned by #2565; residual production recovery remains owned
by #2533 and #2596.

Integrate #2608's nested artifact request and shared paging helper without changing the production
`artifacts.get` contract. Move console-part polling into non-collected test support. The poll scans
each new immutable part at most once, continues when a part lacks the unique marker, and returns
the first marker-bearing part with its full text. Tied artifact timestamps and listing order do not
select the result. Keep the five-minute deadline and five-second interval in the live caller while
allowing focused tests to use bounded injected values. Do not change capture, rotation, workflow
selection, or production artifact behavior.

## Failure model

- Fixture setup failure starts no case and therefore creates no new worker cleanup obligation.
- PostgreSQL restoration failure is reported and gates database-dependent fleet cleanup.
- Fleet cleanup failure is reported without suppressing an existing test failure.
- A vanished cgroup path is accepted only as process-absence evidence; unit identity and retained
  lifecycle facts continue to come from systemd properties and PostgreSQL.
- An unexpected permission, decoding, or malformed-content error remains visible.
- A new immutable console part without the marker is an intermediate observation and is checked
  only once; absence of a marker-bearing part through the deadline remains a failure.
- Artifact-list ordering among tied timestamps is not trusted; each newly observed candidate in
  the bounded listing is eligible for content inspection.
- Production capture and rotation failures remain outside this test-only integration and are not
  converted into polling success.

## Success

1. Focused tests prove populated, empty, blank, disappeared, and malformed cgroup observations.
2. Recovery-helper tests preserve a primary error and record the secondary message and traceback.
3. Isolated pytest subprocesses prove teardown runs after startup and post-start assertion failures,
   and prove a body failure plus teardown failure are both reported.
4. The basic cases still assert full terminal evidence on their successful path.
5. Expected dependency failures are decoded and asserted rather than raised by the subprocess
   wrapper, restart setup waits for the terminal failed invocation, and outage cleanup proves the
   stop-then-recover sequence.
6. The exact six-case hosted proof passes in one run, including the outage and three recovery cases.
7. Focused guardrails and `just ci` pass without production or workflow changes.
8. Focused paging coverage proves `artifacts.get` receives the nested request on initial and
   continuation reads.
9. Focused polling coverage proves an early new part without the marker is skipped and read only
   once, then a later marker-bearing part is returned regardless of listing order.
10. One exact-head workflow passes both the native KVM and hosted TCG jobs before PR #2607 merges;
    PR #2609 is then closed as superseded by that merged integration.

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
- **Artifact request paging — Mode: focused-test.** Run `uv run python -m pytest
  tests/integration/live_stack/test_spine.py -q`. Reverting the nested request makes the fake
  client observe flat `artifact_id` and `byte_offset` arguments; the implementation sends both
  pages inside `request` and returns their concatenated plaintext.
- **Marker-bearing part selection — Mode: focused-test.** Run `uv run python -m pytest
  tests/integration/live_stack/test_console_parts.py -q`. Before implementation the support
  function is absent; afterward a scripted listing sequence proves the first new non-marker part
  is not returned or fetched twice and the later marker-bearing part supplies both id and text.
- **Combined live gate — Mode: live-test.** Dispatch `live.yml` for the exact integrated head and
  require both jobs to complete successfully, including all six systemd cases and the native
  console-parts assertion.
