# Recycle an exhausted preactivation authority teardown (#3617)

## Scope and authority

Campaign scope for issue #3617, token `q3617-7b9ad966`, frozen in the issue's `WORK:SCOPE`
annotation. The operator approved these exclusions on 2026-11-13: a reconciler-side terminalizer,
the binding-mismatch exit (#3616), changing `jobs.cancel`, recycling from automatic callers,
stranded `runs.boot` recovery (#3596), and the `authority_system_not_attachable` wording. No
migration and no new ADR; the decision is recorded as an ADR-0620 amendment.

## Problem

An authority-owned System's preactivation teardown is the job that carries the
`authority_system_v1` marker with operation `preactivation-teardown`. When the provider authority
fails on every attempt without a terminal receipt, the worker leaves the job `running`
(`jobs/worker.py` `_finalize_handler`, "marked authority System job ... failed without a terminal
receipt"). `claim_worker_job` reclaims a lapsed `running` row only while
`attempt < max_attempts` (migration 0112), so after the third attempt the row stays `running`
with a lapsed lease and is never claimed again.

No public call recovers it:

- `systems.teardown` and `ops.force_teardown` reach `enqueue_preactivation_teardown`
  (`services/systems/authority_owned.py`), which returns any prior row that is not `failed`
  without calling the queue;
- `jobs.cancel` refuses the job (`authority_system_preactivation_teardown_not_cancelable`);
- the reconciler's job repair skips marked payloads;
- release stays fenced until the authority's absence receipt is consumed.

The System and its Allocation stay stranded even after the operator fixes the cause.

## Decision

`systems.teardown` on a System without an activation recycles an exhausted, lease-lapsed
preactivation teardown, as it already does for the post-activation authority teardown (ADR-0620
amendment of 2026-09-28, #2889).

1. `enqueue_preactivation_teardown` stops deciding recyclability itself. After the marker check it
   always calls `queue.enqueue_with_status` with the caller's `recycle` policy, so the queue's
   `UPDATE` alone judges which prior row may restart. Under `NEVER` or `FAILED` a `running`,
   `queued`, `succeeded` or `canceled` row comes back unchanged, which is the old replay.
2. The function accepts only `NEVER`, `FAILED` and `FAILED_OR_LAPSED_EXHAUSTED` and raises
   `ValueError` for any other policy. `TERMINAL` and `TERMINAL_OR_CANCELED` would let the queue
   restart a `succeeded` teardown whose receipt was already consumed, which ADR-0620 forbids. No
   caller passes them today; the check keeps that true without relying on the short-circuit.
3. `systems.teardown`'s preactivation branch (`_enqueue_preactivation_authority_teardown` in
   `mcp/tools/lifecycle/systems/admin.py`) passes `FAILED_OR_LAPSED_EXHAUSTED` instead of
   `FAILED`. The queue then also restarts a `running` row with `attempt >= max_attempts` and
   `lease_expires_at` before the database clock. That row keeps its attempt counter and gains
   another `max_attempts` budget (existing `enqueue_with_status` behavior), so the dead attempt's
   number is never reused. The existing `on_recycle` audit (`teardown:recycled`) records the
   requester, because `changed` is true for this restart too.
4. Every other caller keeps its policy: the `jobs.cancel` fence, the reconciler orphan repair and
   investigation force-close pass `NEVER`; `ops.force_teardown` passes `FAILED` through
   `enqueue_control_teardown`, whose single `recycle` argument also governs ordinary teardown
   rows.

The release fence is unchanged: `request_authority_system_preactivation_teardown` still binds the
same job (`replay`), and release still waits for the consumed absence receipt.

## Why the restart is safe

- A dead final attempt cannot commit after its lease lapses: `finalize_authority_system_attempt`
  (migration 0149) returns `superseded` unless the job is `running` with the caller's worker,
  attempt, and an unexpired lease. Its heartbeat is refused once the row is `queued`.
- A final attempt whose heartbeat renewed its lease does not match the `UPDATE`, so it is returned
  unchanged; the lease is judged by the same statement that restarts the row, on the database
  clock.
- The restarted row is in the state an ordinary lease lapse at `attempt < max_attempts` produces:
  the next claim takes over with the same operation identity, as every retry already does.
- `systems.teardown` runs under the per-System advisory lock, as before.

## Failure model

1. Actors and deployments:
   - a project `admin` calling `systems.teardown` after fixing the authority fault;
   - the worker finalizer and the reconciler, which never pass a recycling policy here;
   - deployments: any kdive stack with authority-owned Systems (local and remote authority).
2. Invariants and assets at stake:
   - release stays refused until the preactivation-absence receipt is consumed;
   - a receipt is consumed once, and a `succeeded` teardown is never restarted;
   - a live final attempt is never restarted under a worker that still holds its lease;
   - automatic callers never re-spend the attempt budget.
3. Accepted failure classes:
   - an operator who calls `systems.teardown` before fixing the cause spends another three
     attempts and must call it again; bounded by `max_attempts` per request, as for the #3530
     `failed` recycle;
   - the final attempt recorded a `preactivation-absent` receipt and died before finalizing it,
     and the reconciler's teardown repair has not consumed it yet (for example it reports
     "still has core cleanup"). The row matches the recycle predicate. The recycled row is
     `queued`, and `claim_worker_job` will not claim it while that receipt is unconsumed (claim
     fence, migration 0149). The repair still tears the System down, but it marks only a
     `running` job `succeeded`, so the queued row is later claimed, refused `superseded` against
     the `torn-down` owner, and stays unsettled on a `torn_down` System. Bounded: release reads no
     job state and still follows the repair; the cost is one stray job row. The server cannot read `authority_system_attempts` (migration 0149 revokes
     it), so a guard needs a migration; the runbook tells the operator to check the reconciler
     log first. Reported as a follow-up candidate;
   - `ops.force_teardown` still replays an exhausted `running` preactivation teardown; the
     supported path is `systems.teardown`, and changing the break-glass policy would also change
     ordinary teardown rows. Reported as a follow-up candidate.
4. Covered elsewhere:
   - a teardown that cannot succeed because the recorded binding mismatches: #3616;
   - stranded `runs.boot` recovery: #3596;
   - retained-quarantine receipts at exhaustion: migration 0188 ends the job `failed`, already
     recycled by `FAILED`.

## Success

- With the marked teardown `running`, `attempt = max_attempts` and a lapsed lease,
  `systems.teardown` returns the same job `queued`, with `attempt` unchanged and `max_attempts`
  set to `attempt + max_attempts`, and writes one `teardown:recycled` audit row for the requester.
- With the lease still live, `systems.teardown` returns the job unchanged and writes no audit row.
- `enqueue_control_teardown` with `NEVER` returns the exhausted row unchanged in both cases.
- After the restart, the absence receipt is still unproven; a new claim and a
  `preactivation-absent` receipt finalize the job `succeeded` and the System `torn_down`, and the
  absence is then proven.
- `enqueue_preactivation_teardown` raises `ValueError` for `TERMINAL` and `TERMINAL_OR_CANCELED`.
- `exhausted-authority-system-provision.md` "Limits" states the three-attempt stop and the
  `systems.teardown` restart and links the #3611 subsection of `live-testing.md`; that subsection
  no longer says nothing restarts the teardown.

## Validation

Real-Postgres tests in `tests/services/systems/test_authority_admission.py`:

- `test_exhausted_preactivation_teardown_recycles_only_after_lease_lapse[lapsed]` and `[live]`
  cover the first four Success bullets.
- `test_preactivation_teardown_refuses_terminal_recycle` covers the `ValueError`.

The existing preactivation tests (`failed` recycle audit, capped teardown restart, cancel fence)
must stay green. The runbook and ADR text have no executable consumer; they are reviewed.
