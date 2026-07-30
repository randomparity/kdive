# ADR 0500 — A Run's terminal transition commits with its job's dead-letter

- **Status:** Accepted
- **Date:** 2026-07-29
- **Depends on:** [ADR-0018](0018-job-queue-worker-execution.md) (decision 7 — the worker holds
  no transaction *across the handler*; §2 shows why this transaction is not one),
  [ADR-0492](0492-system-records-its-own-failure-category.md) (the Systems fix whose
  Consequences disclosed this residual, and whose §5 rejection does not reach it),
  [ADR-0483](0483-non-retryable-category-dead-letters-a-job.md) (the category-driven terminal
  decision, which makes the window reachable on the first attempt of three),
  [ADR-0016](0016-repository-layer-locks-idempotency.md) (the advisory-lock scopes whose
  acquisition order §4 has to respect).
- **Leaves standing:** [ADR-0128](0128-remote-provision-vm-creation-gaps.md) (its rejection of
  failing a terminal-state re-entry) and
  [ADR-0454](0454-systems-get-resolves-the-failing-job-category.md) (the failing-job
  attribution; nothing here changes what a job row means).

## Context

The worker's failure path makes two writes, and until now nothing spanned them.

`Worker._run_handler` and `Worker.run_once`'s no-handler arm each acquire one fresh pool
connection and then call `queue.fail` — which opens its own `conn.transaction()` and **commits**
— followed by `_compensate_run_failure`, which opens a **second** transaction for `UPDATE runs
SET state = 'failed', failure_category = …, failing_job_id = …`. Two transactions, one
connection, sequential. Anything landing between them leaves the first committed and the second
never run.

### The loss shape, stated precisely

ADR-0492's Consequences described this residual as leaving "a Run without its category". That
wording is imprecise, and the imprecision hides that the Runs case is strictly worse than the
Systems case it was disclosed beside.

No handler transitions a Run to `failed`. `RunState.FAILED` has exactly two writers in `src/`:
this compensation and `repair_abandoned_jobs`. So the lost write is not a *column* on a row that
is already terminal — it is the Run's **entire terminal transition**. What the window leaves is a
Run still in `created`/`running`, with `failure_category` and `failing_job_id` both NULL, beside
a job that is durably `failed`.

That state is unreachable by every repair the platform has:

- `repair_abandoned_jobs` selects `jobs WHERE state = 'running' AND lease_expires_at < now() AND
  attempt >= max_attempts`. The orphaning job is `failed`; it never matches.
- `queue.dequeue` claims only `queued` rows and lapsed-lease `running` rows, so the reclaim path
  cannot re-derive the failure either.
- No other pass reaches a Run by any other signal. `UPDATE runs` appears at four sites in `src/`;
  the other two are `services/runs/complete_build.py` (`→ succeeded` only) and
  `services/runs/bind.py` (no `state`). `repairs/systems.py`, `repairs/allocations.py`,
  `repairs/console_rotation.py`, `repairs/debug_sessions.py` and every `cleanup/` sweep only read
  or join `runs`; none transitions one, and none is keyed on Run age.

So the Run stays non-terminal **forever**. `runs.get` reports it as still running, `runs.wait`
never settles, and every `RunState.FAILED` branch in the Run renderers is unreachable for it.
Where ADR-0492's shape 1 left a System with a category that was merely *wrong* — `lease_expired`,
a truthful statement about the bookkeeping — a Run is left with no terminal state at all and
nothing that will ever give it one.

### Reaching it needs no process death, and needs no exhausted attempts

`queue.fail` returns; then the compensation's `pg_advisory_xact_lock` or its `UPDATE` can raise —
a lock wait under `lock_timeout`, a statement timeout, a reset connection. The exception escapes
`_run_handler`'s `except Exception` into `_claim_loop`, which catches `Exception` and continues.
The worker survives, having durably dead-lettered a job and durably orphaned a Run.

ADR-0483 makes it reachable on the **first** attempt of three: a non-retryable category
dead-letters at once, so `terminal=True` fires with attempts still remaining, and from that
moment the `failed` job is outside `dequeue`'s reach. The "attempts remaining" path that made
ADR-0492's shape 2 merely a wrong answer has no counterpart here — for Runs it is the same
permanent orphan.

For the same reason, ADR-0492's shape 2 is **not reachable at all** in this codebase: it needs a
handler that re-enters an already-terminal target and early-returns success, which
`TERMINAL_SYSTEM_STATES` gives Systems and nothing gives Runs. No run handler reads the Run's
state to decide whether to run; `RUN_BUILD_TERMINAL` is consulted only by the `runs.bind`
service, which rejects rather than early-returns. §Decision 3 records what replaces it as the
second window worth pinning.

## Decision

### 1. One function, one transaction: `_fail_job_and_run`

Both call sites now call a single `_fail_job_and_run(conn, job, category, terminal=…,
failure_context=…)`, which opens one `conn.transaction()` spanning `queue.fail` and the Run's
transition. `queue.fail`'s own `conn.transaction()` nests as a SAVEPOINT inside it, so the two
writes are one commit: either the job is dead-lettered *and* its Run is `failed` with the
category and the `failing_job_id` pointer, or neither happened.

`_compensate_run_failure` is deleted rather than kept alongside. Folding the pair into one
function is the substance of the fix, not a tidy-up: the defect was that two writes which must
land together were separately callable and separately committing, at two call sites. There is no
longer a call site that can order them wrongly, commit only the first, or omit the second. The
payload lookup and the `runs` statement survive as `_compensation_run_id` and `_mark_run_failed`,
neither of which opens a transaction — they document that the caller owns it.

### 2. This is not ADR-0492 §5's rejected alternative

ADR-0492 §5 rejected "reuse the handler's connection for `queue.fail`, or open a transaction
across both". Every clause of that rejection is about the **handler's** transaction, and none
reaches this change:

- *"It inverts ADR-0018 decision 7."* Decision 7 says the worker holds no transaction **across
  the handler**, because a handler runs 30+ minutes. This transaction opens after the handler has
  returned and after its autocommit dispatch connection has been released, and it spans two
  writes the worker already made itself on its own fresh finalize connection. ADR-0018's stated
  reason for that fresh connection — a handler may have poisoned its own — is preserved exactly.
  The transaction's length is two `UPDATE`s.
- *"It would put a `jobs`-table write inside a domain handler's transaction, making the handler a
  writer of queue state."* No handler is involved. Both writes were already the worker's, on the
  worker's connection.
- *"It still cannot survive the worker being killed."* True for Systems, and the reason is
  specific to Systems: the handler's `System → FAILED` had **already committed**, so a kill after
  it stranded a terminal row with nothing to explain it, and no arrangement of the second write
  could reach back. For Runs there is no earlier committed write to strand — both writes are
  inside the unit — so a kill rolls the pair back. §3 is why that is a recovery rather than
  another loss.

ADR-0492's own Consequences say the Runs writer "is a different writer with a different owner".
That is the distinction: the same window, a different owner, and for this owner the atomic pair
is available where it was not for Systems.

### 3. Rollback *is* the recovery, because `running` is the reapable state

Rolling both writes back leaves the job `running`, `worker_id` still set, `lease_expires_at`
wherever the last heartbeat put it — which is precisely the state the platform already reaps:

- **Attempts remaining.** The lease lapses and `queue.dequeue` reclaims the job. The handler runs
  again, re-derives the same failure, and this attempt commits the pair. The Run ends `failed`
  with the handler's **real** category.
- **Attempts exhausted.** `repair_abandoned_jobs` matches exactly — `running`, lapsed lease,
  `attempt >= max_attempts` — dead-letters the job and transitions the Run in the same
  transaction it already spans.

This is why the atomic pair is sufficient here and why no new observer is needed: the state the
rollback produces is the one two existing mechanisms are already looking for. Nothing has to
learn to find an orphaned Run, because there is no longer an orphaned Run to find.

**Residual, stated rather than claimed away.** On the attempts-exhausted branch the Run's
recorded category degrades to `lease_expired` instead of the handler's real one. That is the same
residual ADR-0492 §4 accepted for a job row, and it is a strict improvement on what it replaces:
a terminal Run whose category is a truthful statement about the bookkeeping, rather than a
non-terminal Run that no repair could reach and that reads as still running for the life of the
deployment. `runs.get`'s existing `failing_job_id`-gated detail already distinguishes a Run with
a pointer from one without.

### 4. The Run's advisory lock moves ahead of `queue.fail`, or the fix deadlocks

Spanning the two writes co-holds locks the old shape released between them, and the naive
ordering is an ABBA deadlock against a real caller.

The Run write takes `advisory_xact_lock(conn, LockScope.RUN, run_id)`, and `queue.fail` row-locks
the `jobs` row. Keeping the statements in their current order inside one transaction would give
**jobs row → RUN advisory**. But `runs.steps`' `_enqueue_step` holds `LockScope.RUN` and *then*
calls `queue.enqueue(recycle_terminal=…)`, whose `UPDATE jobs … WHERE dedup_key = %s` row-locks
**this very job** — the dedup key is `f"{run.id}:{step}"`. That is **RUN advisory → jobs row**. A
`runs.boot(force=True)` or a `runs.install` re-stage concurrent with that job's failure would
deadlock, and Postgres would abort one of them.

So `_fail_job_and_run` acquires the RUN lock **first**, before `queue.fail`, matching the order
every other RUN-scoped writer already uses. Two consequences follow and are accepted:

- The payload's `run_id` is parsed before the fail rather than after it, so a job whose persisted
  payload no longer validates logs its warning on the requeue branch too. Truthful, and earlier.
- The requeue branch now briefly holds a lock it never took. It is one `UPDATE` long, and it
  serializes a Run's requeue against that Run's own step enqueues, which is the correct
  relationship between them.

`repair_abandoned_jobs` is deliberately left holding no advisory lock. It already spans both of
its `UPDATE`s in one transaction, and each is a single guarded statement, so the row locks
suffice; giving it the lock would reintroduce **jobs row → RUN advisory** from the other side.

### 5. The `worker_id` fence is untouched, and it is what keeps a stale worker honest

`queue.fail`'s `WHERE id = %s AND worker_id = %s AND state = %s` is unchanged, and the Run write
still runs only when the job `queue.fail` returns is `failed`. A worker whose lease lapsed
mid-handler, and whose job another worker has since claimed, gets a fence miss: `queue.fail`
returns the job unchanged in `running`, and the Run write is skipped — inside the shared
transaction exactly as it was outside it.

Merging the two writes into one statement — the Run `UPDATE` as a CTE on `queue.fail`'s — was
rejected for this reason. It would either make the Run transition unconditional, or need the
fence spelled a second time in a second place, which is the drift the single fence exists to
prevent.

### 6. Rejected: a reconciler sweep that hunts orphaned Runs

The alternative to atomicity is to leave the writes split and teach the reconciler to find the
orphan afterwards. Rejected on three counts:

- **The orphan carries no pointer.** `failing_job_id` is written by the very statement that was
  lost, so a sweep can only correlate through `jobs.payload->>'run_id'` — an unindexed predicate
  on an append-only table. ADR-0491 indexed `payload->>'system_id'`, not `run_id`.
- **It cannot tell an orphan from ordinary progress.** A Run accumulates `build`, `install` and
  `boot` jobs, and `runs.install`'s re-stage recycles a terminal one back to `queued`. A Run in
  `created` beside a `failed` `boot` job and a fresh `queued` `install` job is healthy, and a
  "newest terminal job failed" rule would fail it.
- **It is a second net for a state the fix makes unreachable**, paid for with another pass in the
  30-second loop.

Pre-existing orphaned Runs are **not** backfilled, on ADR-0492's own reasoning for declining its
backfill: a migration doing this correlation once, at deploy time, over rows an agent has already
read and acted on.

### 7. No migration, and nothing else changes

`runs.state`, `runs.failure_category` and `runs.failing_job_id` all already exist
(`db/schema/0001_init.sql`; the category's CHECK widened by migrations 0004, 0017, 0026, 0028 and
0059). This ADR changes only *when* an existing write commits: no schema, no migration, no config
setting, no dependency, no MCP tool schema, RBAC or exposure change, and no change to any
envelope a caller sees on a Run that failed normally.

## Consequences

- A Run failed by a worker that died — or whose Run `UPDATE` faulted — is no longer orphaned. It
  is either `failed` with the handler's category and a `failing_job_id`, or non-terminal beside a
  `running` job that `dequeue` and `repair_abandoned_jobs` are already looking for. The
  "permanently non-terminal, invisible to every sweep" state is gone.
- Both windows are pinned by tests that drive the real `Worker` against a real database
  (`tests/adversarial/test_run_failure_atomicity.py`), and the fault in the first is **real
  Postgres contention** — a second connection holds a row lock on `runs` while the worker's pool
  runs under `lock_timeout` — rather than a patch over worker internals. Each was confirmed to
  fail against the unfixed code on the assertion that the job is `running` rather than durably
  `failed`, which is the orphaning state itself.
- The lock-order requirement of §4 is pinned by its own test, and that test asserts `queue.fail`
  was never *reached*, not merely that the job survived: a rollback leaves the job `running`
  under either ordering, so the job's state cannot discriminate them. Verified by mutation rather
  than asserted — moving the lock back after `queue.fail` while keeping the transaction reddens
  that test and only that test, and its job-state assertion keeps passing, which is the claim.
- The tests are also mutation-checked in the other direction, because RED-before-GREEN-after alone
  does not rule out a *positive* assertion that holds for the wrong reason. Neutralizing
  `_mark_run_failed` while leaving everything else in place reddens the "the Run ends `failed` with
  the handler's category" assertion on all three columns, and reddens the two contention tests with
  `DID NOT RAISE LockNotAvailable` — which is the useful signal: it proves the fault those tests
  inject is the Run's own `UPDATE`, so the window they claim to open is genuinely open rather than
  incidentally satisfied by the fixture.
- **A new failure mode replaces the old one, and it is the loud kind.** Where a faulting Run
  `UPDATE` used to leave a silently orphaned Run and a finalized job, it now leaves the job
  unfinalized, so the same fault costs one more attempt (or, at the last attempt, a
  `lease_expired` category). A fault that repeats on every attempt therefore ends at
  `lease_expired` rather than at the handler's category — traded for the Run reaching a terminal
  state at all.
- The failure path holds the Run's advisory lock for the duration of two `UPDATE`s, where it
  previously held it for one and took none on the requeue branch. A `runs.boot`/`runs.install`
  enqueue for the same Run can now block on a concurrent failure of that Run's job for that long.
- `queue.fail` is now called inside a caller-owned transaction on the worker path, so its
  `conn.transaction()` is a SAVEPOINT rather than a commit there. Its docstring says so; every
  other caller is unaffected, and the module docstring's "self-commits on any connection" remains
  true of the function in isolation.
- `repair_abandoned_jobs` is unchanged, and so is what a job row means. ADR-0454's attribution,
  ADR-0128's terminal-state re-entry, and ADR-0483's terminal decision all stand.
