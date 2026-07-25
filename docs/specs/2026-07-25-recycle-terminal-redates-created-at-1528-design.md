# `recycle_terminal` re-dates `created_at` (#1528)

- **Issue:** [#1528](https://github.com/randomparity/kdive/issues/1528) — P3 bug
- **ADR:** [ADR-0447](../adr/0447-recycle-terminal-redates-created-at.md)
- **Revises:** [ADR-0442](../adr/0442-rootfs-reclaim-worker-job.md) §6's *first* justification for
  the delete-and-re-insert idiom

## Problem

`queue.enqueue(..., recycle_terminal=True)` revives a settled job in place, resetting `state`,
`payload`, `attempt`, `worker_id`, `lease_expires_at`, `heartbeat_at`, `error_category`,
`result_ref` and `failure_context` — but not `created_at`.

`dequeue` claims with `ORDER BY created_at` under `attempt < max_attempts`. A recycled row
therefore keeps its **original** `created_at`, sorts ahead of everything enqueued since, and — with
`attempt` back at 0 — is eligible again. A job that keeps failing and keeps being recycled is
picked first on every claim, indefinitely: it head-of-line-blocks its dispatch lane instead of
yielding to newer work.

## Blast radius today

Every current `recycle_terminal=True` caller is **caller-triggered**, not swept:

- `mcp/tools/lifecycle/runs/steps.py` — the install/boot re-stage (ADR-0185/ADR-0299)
- `mcp/tools/lifecycle/control/registrar.py` — `control.watch_for_crash` (ADR-0367)
- `mcp/tools/lifecycle/systems/snapshot.py` — the snapshot/restore tools (ADR-0378)

A stuck job re-blocks the lane only as often as an agent or operator retries it, and the retry is
attributable to the caller who saw the failure. That is why this is P3.

The dangerous shape is a *background* recycler on a timer, which would re-block the lane every tick
with nobody watching. #1522 hit exactly that shape (a ~30 s reconciler sweep) and avoided the
primitive entirely: `reconciler/cleanup/gc.py` `_enqueue_rootfs_reclaim` deletes the settled row and
inserts a fresh one — which gets a fresh `created_at` — behind a 5-minute backoff, with
`max_attempts=1`. That was a local workaround, not a fix to the primitive.

(The campaign brief cited PR #1533 as the workaround; #1533 is the upload-deadline change and does
not touch the queue. The delete-and-re-insert landed in **PR #1529** for #1522, ADR-0442 §6.)

## Requirements

- **R1** — A recycled job does not preempt a job enqueued after the recycled job's original
  creation.
- **R2** — A recycled job is still claimed; it queues behind the newer work rather than being
  starved.
- **R3** — Every other `recycle_terminal` invariant holds: reset in place (same row id, no
  duplicate), payload overwritten, in-flight `queued`/`running` untouched, `canceled` untouched
  unless `recycle_canceled`.
- **R4** — No schema change and no migration.

## Options considered

**(a) Re-date `created_at` in the recycle `UPDATE`** — chosen. One clause, no migration (review
added a matching stamp on the `INSERT` so both branches share one clock; see Design). The cost is
that `jobs.created_at` means "when this attempt was queued", not "when the row was first inserted".

**(b) Add a `queued_at` (or priority) column that `dequeue` orders by, keeping `created_at` as
provenance** — semantically cleanest, but needs a migration, which this campaign does not sanction.
Rejected on that ground, not on merit; recorded in ADR-0447 as the revisit path if a
first-insertion timestamp is ever actually needed.

**(c) Document `recycle_terminal` as unsafe for timer-driven callers and sanction the
delete-and-re-insert idiom** — no schema change, but leaves a live footgun in a core primitive for
the next caller to rediscover. Against the repo's "replace, don't deprecate" standard, and it
does not satisfy R1 for the callers that already exist.

## What else reads `jobs.created_at`

Every reader is in `jobs/queue.py` or `jobs/worker.py`; no other module selects the column, and
neither the audit log nor accounting joins it.

| Reader | Effect of re-dating |
|---|---|
| `dequeue` — `ORDER BY created_at` | The fix target. FIFO becomes "by when the attempt was queued". |
| `worker.py` `record_time_to_claim` — `heartbeat_at - created_at` | **Improved.** The metric measures queue wait; today a recycled job reports a bogus wait spanning its entire prior lifetime. |
| `all_recent_jobs` — `ops.jobs_list`, `ORDER BY created_at DESC, id DESC` | A recycled job floats to the top of the operator's newest-first list, which is where a just-requeued job belongs. |
| `recent_jobs` — `jobs.list`, same order plus the `(created_at, id)` keyset cursor | A recycled row can move forward past a page boundary and be missed for the rest of that pagination. `clock_timestamp()` makes a given row's `created_at` move only forward, so a row can be skipped but never duplicated — the same behavior a fresh insert or the sanctioned delete-and-re-insert already produces mid-pagination. The cursor is a boundary, not a snapshot. |
| `latest_succeeded_job_for_system` — newest `succeeded` by `(created_at, id)` | Unaffected: it filters `state = 'succeeded'`, and a recycled job is `queued`. |

`updated_at` is trigger-maintained (`jobs_set_updated_at`) and no consumer reads it, but it is
**not** a fallback record of the recycle — see the Design section below.

## Design

Add `created_at = clock_timestamp()` to the recycle `UPDATE`, and stamp the `INSERT` with the same
expression instead of letting it take the column's `DEFAULT now()`.

**Why `clock_timestamp()` and not `now()`.** `now()` is `transaction_timestamp()`, fixed when the
enclosing transaction begins, and `enqueue` never opens a top-level transaction of its own in
production: `runs/steps.py` and `systems/snapshot.py` open `conn.transaction()` and then block on an
`advisory_xact_lock` before enqueuing, and `control.watch_for_crash` runs on a pooled connection
(autocommit off) whose implicit transaction opened several reads earlier. `now()` would date the
revived job to *before* the lock wait, leaving it ahead of everything another connection enqueued
during that wait — R1 violated exactly under contention. The same reasoning applies to a *first*
enqueue, so both statements stamp from one clock rather than leaving the two branches of `enqueue`
on different ones. `clock_timestamp()` is always at or after `transaction_timestamp()`, so a given
row's `created_at` only ever moves forward — the per-row property the keyset-cursor row above
depends on. It is the same database clock either way, so no worker clocks need to agree.

`updated_at` is not a fallback record of the recycle: its trigger stamps `now()` while this stamps
`clock_timestamp()`, so a recycled row can read `created_at > updated_at`, and the value is the
transaction's start rather than the recycle instant. Nothing in `src/` reads it today; correcting
the skew would mean changing the shared `set_updated_at()` trigger, i.e. a migration, which is out
of scope.

The `WHERE dedup_key = %s AND state = ANY(%s)` fence is unchanged, so the re-dating reaches only
rows the recycle already resets: a row that was just inserted is `queued` and does not match, and
an in-flight or (absent `recycle_canceled`) `canceled` row is left alone.

The `UPDATE` gains `RETURNING id`, and a matched row logs one `INFO` line. Re-dating removes the
last in-row evidence that a job has been recycled (`attempt` is already reset, failure fields and
`result_ref` cleared, payload overwritten), so without this a churning `dedup_key` is invisible;
nothing else counts recycles. The line is emitted after the `conn.transaction()` block, but it is
an **upper bound** rather than proof: for a production caller that block is a SAVEPOINT inside the
caller's transaction, so releasing it is not a commit and the caller can still roll back.

`authorizing`, `max_attempts`, `kind` and `dispatch_lane` stay untouched — they describe the slot,
not the attempt. That asymmetry is pre-existing, but re-dating is what makes the timestamp and the
attribution describe different events, so it is now documented in the `enqueue` docstring.

The net effect is that `recycle_terminal` becomes equivalent to the delete-and-re-insert a caller
would otherwise hand-roll, minus the row churn and minus losing the row id.

## Accepted cost

All three callers are interactive retries, and the lane is FIFO across kinds, so a re-staged install
now queues behind everything admitted while the run was settled — possibly a long `build`. Accepted:
the delta is bounded and usually small (a re-stage's original `created_at` is recent), and it is the
correct direction — a retrying agent should not outrank work that has waited longer. An opt-in
keyword was rejected as a speculative flag that leaves the footgun armed.

For `systems.restore` the wait is **state-fenced**, not merely perceived: it sets the System to
`RESTORING` before enqueuing in the same transaction, and `delete_snapshot` rejects with
`system_restoring` meanwhile. No caller passes `dispatch_lane`, so every kind shares the single
`default` lane, and nothing times out a queued job — so a retried restore can sit behind a long
`build` with the System fenced. Accepted here and filed as a follow-up: giving the state-fenced
kinds their own `dispatch_lane` needs no migration (the column and `accepted_lanes` already exist)
and is orthogonal to the ordering bug, so it should not ride on this change (#1538).

## Test plan

`tests/jobs/test_queue.py::test_enqueue_recycle_terminal_does_not_preempt_newer_work` — the R1/R2
regression test:

1. Dead-letter a job, then back-date its `created_at` an hour (deterministic, no clock race).
2. Enqueue a second job — after the first job's original creation.
3. Recycle the first; assert its `created_at` is now past the second's, and that it is the same row.
4. `dequeue` claims the **newer** job first (R1), and a second `dequeue` claims the recycled one
   (R2 — behind, not starved).

`tests/jobs/test_queue.py::test_enqueue_recycle_terminal_redates_past_a_concurrent_enqueue` covers
the production transaction shape the first test cannot: the recycle runs on a **non-autocommit**
connection inside one explicit `conn.transaction()` opened *before* a second connection enqueues the
competing job. It passes under `clock_timestamp()` and fails under `now()`, which is what pins the
clock choice.

The existing `recycle_terminal` tests (reset-in-place, in-flight preserved, succeeded-with-new-payload,
canceled-only-when-opted-in) cover R3 and are unchanged.

## Non-goals

- No `dequeue` tiebreaker on `id` — and this change makes one *less* necessary, not more. Under
  `DEFAULT now()` two jobs enqueued in the same transaction shared a `created_at`; stamping the
  `INSERT` with `clock_timestamp()`, which advances per statement, means they no longer tie at all.
  Any residual tie still makes progress under `FOR UPDATE SKIP LOCKED`, and `id` is a random uuid,
  so ordering by it would not be meaningfully fairer than ordering arbitrarily.
- `gc.py`'s delete-and-re-insert is **not** replaced by `recycle_terminal`. Its second
  justification — a 5-minute backoff that also keeps the `failed` row inspectable, with
  `max_attempts=1` because the sweep is the retry loop — is independent of this bug and still
  stands. Only its now-stale first justification is corrected.
