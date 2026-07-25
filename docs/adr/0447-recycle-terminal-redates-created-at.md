# ADR 0447 — `recycle_terminal` re-dates `created_at`, so a revived job queues at the back

- **Status:** Accepted
- **Date:** 2026-07-25
- **Revises:** [ADR-0442](0442-rootfs-reclaim-worker-job.md) §6's *first* justification for the
  delete-and-re-insert reclaim idiom ("re-issued (deleted and re-inserted, so `created_at` is
  re-dated and a faulting reclaim cannot head-of-line-block the `created_at`-ordered dequeue"). The
  primitive now re-dates on its own, so that idiom no longer carries the fairness burden. ADR-0442's
  *second* justification — the 5-minute backoff, which also keeps the `failed` row inspectable, with
  `max_attempts=1` because the sweep is the retry loop — is unchanged and is what keeps the idiom in
  place. Every other ADR-0442 decision is untouched.
- **Depends on:** [ADR-0018](0018-job-queue-worker-execution.md) (the durable `jobs` queue, its
  `dedup_key` admission and its `created_at`-ordered `dequeue`),
  [ADR-0185](0185-retry-terminal-failed-step.md) and
  [ADR-0299](0299-install-cmdline-iteration.md) (the two `recycle_terminal` semantics),
  [ADR-0367](0367-out-of-band-crash-watch.md) (`recycle_canceled`).
- **Spec:** [`../specs/2026-07-25-recycle-terminal-redates-created-at-1528-design.md`](../specs/2026-07-25-recycle-terminal-redates-created-at-1528-design.md)

## Context

`queue.enqueue(..., recycle_terminal=True)` revives a settled job in place. It resets `state`,
`payload`, `attempt`, `worker_id`, `lease_expires_at`, `heartbeat_at`, `error_category`,
`result_ref` and `failure_context` — everything except `created_at`.

`dequeue` claims with `ORDER BY created_at` under an `attempt < max_attempts` predicate. So a
recycled row kept its **original** `created_at`, sorted ahead of everything enqueued since, and —
with `attempt` back at 0 — was eligible again. A job that kept failing and kept being recycled won
every claim, indefinitely: it head-of-line-blocked its dispatch lane rather than yielding to newer
work (#1528).

Every `recycle_terminal` caller today is caller-triggered — the install/boot re-stage
(`runs/steps.py`), `control.watch_for_crash` (`control/registrar.py`), the snapshot/restore tools
(`systems/snapshot.py`) — so the lane re-blocks only as often as an agent or operator retries, and
the retry is attributable. That is why this was filed P3, not higher.

The shape that makes it acute is a *background* recycler on a timer, which re-blocks the lane every
tick with nobody watching. #1522 hit exactly that (a ~30 s reconciler sweep) and avoided the
primitive rather than fixing it: `_enqueue_rootfs_reclaim` deletes the settled row and inserts a
fresh one, so the reclaim gets a fresh `created_at` (PR #1529, ADR-0442 §6). That left the ordering
asymmetry live in the primitive for the next caller to rediscover — which the repo's
"replace, don't deprecate" standard argues against.

## Decision

The recycle `UPDATE` sets `created_at = now()` alongside the fields it already resets. A revived
job takes its place at the **back** of its lane.

`now()` is `transaction_timestamp()`, the same clock the `INSERT`'s `DEFAULT now()` uses and the
same clock `dequeue` compares leases against, so the insert and recycle paths stay consistent and
no worker clocks need to agree. The `WHERE dedup_key = … AND state = ANY(…)` fence is unchanged, so
re-dating reaches only rows the recycle already resets: a just-inserted row is `queued` and does not
match, and an in-flight or (absent `recycle_canceled`) `canceled` row is untouched.

`recycle_terminal` thereby becomes equivalent to the delete-and-re-insert a caller would otherwise
hand-roll — minus the row churn, and minus losing the row id that `jobs.get` and the tool trail
reference.

The accepted cost: **`jobs.created_at` now means "when this attempt was queued", not "when the row
was first inserted".** Nothing outside `jobs/queue.py` and `jobs/worker.py` selects the column, and
neither the audit log nor accounting joins it. Of the readers that exist:

- `worker.py`'s `time_to_claim` telemetry (`heartbeat_at - created_at`) is **improved** — it is
  measuring queue wait, and today reports a bogus wait spanning a recycled job's entire prior
  lifetime.
- `ops.jobs_list` / `jobs.list` order newest-first, so a recycled job floats to the top, which is
  where a just-requeued job belongs.
- `jobs.list`'s `(created_at, id)` keyset cursor can miss a row that moves forward past a page
  boundary mid-pagination. `created_at` only moves forward, so a row can be skipped but never
  duplicated — the same behavior a fresh insert, or the already-sanctioned delete-and-re-insert,
  produces. The cursor is a boundary, not a snapshot.
- `latest_succeeded_job_for_system` filters `state = 'succeeded'` and so never sees a recycled row.

`updated_at` stays trigger-maintained and, with the audit trail, remains the change-history record.

## Alternatives considered

**An explicit `queued_at` (or priority) column that `dequeue` orders by, keeping `created_at` as
true first-insertion provenance.** Semantically the cleanest: the two facts stop sharing one
column. Rejected because it needs a migration for a P3 whose only surviving cost is a provenance
field nothing reads — a new column, a backfill, and a second timestamp for every future queue reader
to reason about, bought for a fact with no consumer. If a first-insertion timestamp ever acquires a
real consumer (age telemetry, dead-letter reporting, billing), this is the revisit path and this ADR
is what it supersedes.

**Document `recycle_terminal` as unsafe for timer-driven callers and sanction delete-and-re-insert
as the pattern for them.** No schema change, but it keeps a footgun in a core primitive and does not
help the caller-triggered callers that already exist — a re-stage loop can still starve a lane, just
more slowly. Rejected.

**A tiebreaker on `id` in `dequeue`'s `ORDER BY`.** Out of scope: two jobs enqueued in the same
transaction already tie via `DEFAULT now()`, the recycle does not make that worse, and `FOR UPDATE
SKIP LOCKED` keeps a tie making progress.

## Consequences

- A repeatedly-recycled job can no longer starve its lane; it is re-queued behind the work admitted
  while it was settled, and is still claimed.
- `jobs.created_at` is an attempt-queued timestamp, not a first-insertion timestamp. Any future
  reader wanting first-insertion provenance needs the `queued_at` split above.
- `gc.py`'s delete-and-re-insert stays, now justified solely by its retry backoff; its stale
  fairness rationale is corrected in place.
- No schema change, no migration, no MCP/RBAC surface, not an AI surface.
- Regression coverage: `tests/jobs/test_queue.py::test_enqueue_recycle_terminal_does_not_preempt_newer_work`.
