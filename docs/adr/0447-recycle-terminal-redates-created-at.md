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

The recycle `UPDATE` sets `created_at` to the database clock alongside the fields it already resets.
A revived job takes its place at the **back** of its lane.

The stamp is **`clock_timestamp()`, not `now()`, on both statements** — the recycle `UPDATE` and the
`INSERT`, which now stamps explicitly rather than taking the column's `DEFAULT now()`, so `enqueue`
uses one clock throughout. `now()` is `transaction_timestamp()`, fixed when the enclosing
transaction begins — and `enqueue` never opens a top-level transaction of its own in
production. The re-stage (`runs/steps.py`) and the snapshot tools (`systems/snapshot.py`) open
`conn.transaction()` and then *block* on an `advisory_xact_lock` before enqueuing, and
`control.watch_for_crash` runs on a pooled connection (autocommit off) whose implicit transaction
opened several reads earlier. Stamping the transaction's start would date the revived job to before
the lock wait, so everything another connection enqueued *during* that wait would still sort behind
it — the preemption this ADR exists to end, returning precisely under the contention that makes
head-of-line blocking matter. That argument holds for a *first* enqueue no less than for a recycle —
an insert dated to the start of its own lock wait preempts the same way — which is why the `INSERT`
stamps explicitly too rather than leaving one branch of the function on the other clock.
`clock_timestamp()` is the same database clock (so no worker clocks need to agree) and is always at
or after `transaction_timestamp()`, so **a given row's `created_at` only ever moves forward** — the
per-row property the keyset-cursor argument below rests on.
`tests/jobs/test_queue.py::test_enqueue_recycle_terminal_redates_past_a_concurrent_enqueue` drives
the recycle on a non-autocommit connection in one explicit transaction and fails under `now()`.

The `WHERE dedup_key = … AND state = ANY(…)` fence is unchanged, so re-dating reaches only rows the
recycle already resets: a just-inserted row is `queued` and does not match, and an in-flight or
(absent `recycle_canceled`) `canceled` row is untouched.

`authorizing`, `max_attempts`, `kind` and `dispatch_lane` are deliberately **not** reset. They
describe the job's slot, not the attempt; `authorizing` in particular stays with the principal who
first enqueued the job, so a re-dated `created_at` must not be read as the recycling principal's
action time. (A caller that needs the new principal on the record audits its own tool invocation.)
This asymmetry is pre-existing; it is recorded here because re-dating `created_at` is what makes the
timestamp and the attribution describe different events.

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

`updated_at` is **not** the fallback record it might look like. Its trigger stamps `now()` while the
recycle stamps `clock_timestamp()`, so a recycled row can read `created_at > updated_at`, and the
value it holds is the enclosing transaction's start — not the recycle instant, which is the one
moment an operator investigating a churning `dedup_key` wants. Nothing in `src/` reads
`jobs.updated_at` today, so no consumer breaks; the caller's audit entry and the log line below are
the faithful records. Fixing the skew would mean changing the shared `set_updated_at()` trigger,
i.e. a migration, which is out of scope here.

There is a real observability **loss**, not only improvement. An old `created_at` was the last
in-row evidence that a job had been recycled at all: `attempt` is already reset, the failure fields
and `result_ref` cleared, the payload overwritten. Re-dated, a row that has churned a hundred times
is indistinguishable from a first enqueue, and nothing counts recycles (`kdive_job_retries_total` is
the non-terminal-requeue counter, ADR-0191 §I, and does not fire here). The recycle `UPDATE`
therefore gains `RETURNING id` and logs one `INFO` line per actual recycle (job id, kind,
`dedup_key`) — no schema, no new metric, and a thrashing `dedup_key` says so in the log rather than
only in the lane-depth gauge.

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

**A tiebreaker on `id` in `dequeue`'s `ORDER BY`.** Out of scope, and *less* needed after this
change than before it: stamping the `INSERT` with `clock_timestamp()` — which advances per statement
— means two jobs enqueued in the same transaction no longer share a `created_at` at all, where under
`DEFAULT now()` they did. Any residual tie still makes progress under `FOR UPDATE SKIP LOCKED`, and
`id` is a random uuid, so ordering by it would not be meaningfully fairer than ordering arbitrarily.

## Consequences

- A repeatedly-recycled job can no longer starve its lane; it is re-queued behind the work admitted
  while it was settled, and is still claimed.
- **The three callers that exist pay a latency cost.** All of them are interactive retries, and the
  lane is FIFO across kinds, so a re-staged install now queues behind everything admitted while the
  run was settled — possibly a long `build` — where before it was serviced next. This is worth
  stating plainly because the hazard argued above is sharpest for a *timer-driven* caller, and the
  one component with that shape (#1522's reclaim sweep) deliberately does not use the primitive and
  still will not. The trade is accepted on two grounds: the cost is bounded and usually small (a
  re-stage's original `created_at` is recent, so the job was not jumping far ahead to begin with),
  and it is the correct direction — an agent that retries in a loop should not be able to outrank
  work that has been waiting longer, which is the same fairness rule ADR-0018 already sets for
  first-time enqueues. Scoping the re-date to an opt-in keyword was rejected: it is a speculative
  flag for a caller that does not exist, and it leaves the footgun armed for the one that will.
- **For `systems.restore` that wait is state-fenced, not merely perceived.** `systems.restore`
  sets the System to `RESTORING` *before* enqueuing, in the same transaction, and `delete_snapshot`
  rejects with `system_restoring` for the duration — so the added queue time is spent with the
  System unusable, not just with the agent waiting. Two facts make the window unbounded rather than
  short: no caller anywhere passes `dispatch_lane`, so every kind (`build`, `provision`, `install`,
  `boot`, `snapshot`, `restore`, `reclaim_investigation_rootfs`) shares the single `default` lane;
  and nothing times out a *queued* job (`repair_abandoned_jobs` reaps only `running` rows with a
  lapsed lease). A retried restore can therefore sit behind a long `build` with the System fenced.
  This is accepted here rather than fixed, because the fix is orthogonal to the ordering bug and
  should not ride on it: putting the state-fenced kinds on their own `dispatch_lane` needs no
  migration (the column and `accepted_lanes` already exist) and is filed as [#1538](https://github.com/randomparity/kdive/issues/1538).
- `jobs.created_at` is an attempt-queued timestamp, not a first-insertion timestamp. Any future
  reader wanting first-insertion provenance needs the `queued_at` split above.
- `gc.py`'s delete-and-re-insert stays, now justified solely by its retry backoff; its stale
  fairness rationale is corrected in place.
- No schema change, no migration, no MCP/RBAC surface, not an AI surface.
- Regression coverage: `tests/jobs/test_queue.py::test_enqueue_recycle_terminal_does_not_preempt_newer_work`.
