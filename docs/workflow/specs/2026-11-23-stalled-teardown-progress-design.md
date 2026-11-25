# Bounded stalled-teardown candidate progress

## Problem and authority

The actual `Reconciler.run_once` entry point repeatedly sees the same 100 skipped Systems and
never admits the valid 101st ordinary teardown. A healthy-only control queues it immediately.
The user authorizes internal refactors and preservation of public APIs; the current queue owns
this candidate fairness defect. [ADR-0842](../../adr/0842-stalled-teardown-repair-keyset-progress.md)
records the policy. Task72's authority-receipt ordering and automatic replay budgeting are
separate owners and mechanisms, not completed proof for this selector.

## State and runner wiring

Add a small mutable slots dataclass `StalledTeardownProgress` in `reconciler/repairs/systems.py`
with `after: UUID | None = None`. It stores the last ordinarily inspected System, not a lease,
receipt or durable progress claim. Add optional `progress` to `repair_stalled_tearing_down_systems`;
only omission creates one-shot progress. Keep its existing `warnings` argument and behavior.

Add `stalled_teardown_progress` as the third reference in loop.py's private `_RepairState`.
Add the same optional keyword to `_repair_plan` and `reconcile_once`; preserve their existing
cursor/warning arguments. A missing progress object is created per plan; explicit supplied state
is retained even at `after=None`. `Reconciler` owns a fresh instance and supplies it from
`run_once`. Only the existing stalled callback receives it. Keep catalog arity, other factories,
configuration, order, optional-port gating, counts/reporting and repair error isolation unchanged.

## Candidate read and bounded wrap

Extract the existing selection into `_stalled_teardown_candidates(conn, *, after=None)`.
Keep its transaction/cursor framing, original state and blocking-job predicates and parameters,
System-ID order and 100 limit. Add only `(%s::uuid IS NULL OR s.id > %s)` with the same hint twice.
Return typed UUIDs, preserving UUID zero. No eligibility filter is relaxed or moved outside its
current authority boundary.

The repair reads the tail after `progress.after`. Only an empty tail with a prior non-null hint
permits one wrapped lookup with no hint. Never fill a partial tail with another page. Do not
write progress or warning maps before those lookups succeed. If both ranges are empty, clear the
hint and prune the stalled warning map to the empty page. Otherwise process only that page.

## Candidate transaction and checkpoint

Extract the current System-locked body into `_repair_stalled_teardown_candidate`, taking the
connection, System UUID and warning history and returning the existing admission boolean.
Replace guarded `continue` statements with false returns. Preserve the System row/state re-read,
queued/running/canceled job guard, both authority marker keys, latest activation/settlement guard,
warning helper calls, typed payload, project authorizing fields, dedup key and terminal recycle
policy. Return the admission result only after transaction exit; early false returns still exit
that context normally. No new worker, role, lease, cancellation or provider policy.

For each candidate, await that helper inside the existing `except Exception` isolation boundary.
A caught exception logs the same warning prefix/arguments/traceback but says retrying on a later
pass. Set the hint after the ordinary result or handled error, then count/log committed admission.
Do not use `finally` or pre-write a page hint: cancellation must retain the last completed
candidate, including when waiting for the next System lock. Page read failures propagate before
checkpoint or pruning. Keep the remainder of the loop executor unchanged.

## Warning and documentation accuracy

Keep current selected-page pruning and per-cause warning keys; do not retain an unbounded backlog
cache. Visiting another page can evict a prior warning and cause it again on wrap. State that
limit explicitly in the repair docstring/failure model rather than claiming lifetime suppression.
Correct the catalog comment saying automatic replay is already bounded; its current policy is
unchanged here. Correct the reused `_seed_completed_activation` fixture docstring: cleanup is
complete, but no settlement record exists, so it remains a valid unsettled-activation blocker.
No fixture interface rename or weakened assertion is needed.

## Failure model

- The hint is advisory and per runner. Database checks under the System lock own safety.
- A fixed finite skipped/failing prefix yields over repeated normal passes. There is no retry
  deadline: restart resets hints, lower/new rows wait for wrap, and continuing insertions or
  overlapping manual passes can postpone or repeat inspection. Durable deduplication remains.
- Warning suppression covers the current selected page. Page eviction, wrap or restart may warn
  again. The 100-row page bound and current pruning are retained, not a persistent log guarantee.
- Page lookup failure retains prior progress/history; candidate failure advances only that
  inspected System. Cancellation retains the last completed checkpoint and rolls back its own
  interrupted transaction. An uncertain admission remains fenced by existing durable job guards.
- Automatic terminal replay allowance is not fixed here. The separate queued policy change owns
  durable request budgeting and explicit operator recovery.

## Validation

Promote the actual 101-System proof before implementation. Cover both authority markers and
unsettled activations with explicit settlement absence, plus a healthy-only control. Use real
runner instances sharing config/pool to detect shared hints. Check wrap after changing an early
candidate's eligibility, zero UUID, one nonempty page and empty reset, without enlarging limits.

Hold the first 100 System locks in another real transaction and use a short connection-local
lock timeout: caught errors must still yield to the unlocked successor. Hold the second System
lock to stop a real pass after its first committed admission; observe the actual PostgreSQL
advisory wait, cancel and drain the task, verify prior checkpoint/transaction recovery, then retry.
Use an actual blocked initial SELECT to prove lookup failure retains supplied progress/history.
All blockers, connections, tasks and fixture state must be released in `finally`/contexts.

Inject first-page reset, missing checkpoint, shared hint, no wrap, early checkpoint, query-reset
and lost warning/cursor reference faults individually; require named failures and byte restoration.
Rerun the original private proof unchanged and the existing 158-test warning/loop/external-boot
baseline plus new tests. Compare original selection/guarded candidate semantics and whole loop
AST outside declared progress wiring/comment, all136 tool metadata/CLI/reference/constants, then
lint/whole types/docs/records/hooks/build and scoped independent code/security reviews.
