# ADR 0492 — A failed System records its own failure category, atomically with the transition

- **Status:** Accepted
- **Date:** 2026-07-29
- **Depends on:** [ADR-0454](0454-systems-get-resolves-the-failing-job-category.md) (the
  failing-job attribution this keeps as the fallback, and whose Consequences named this issue as
  the trigger to revisit its §1),
  [ADR-0118](0118-wait-on-resource-mechanisms.md) (the derived `retryable` field — a pure
  function of the category, which is why a wrong category is an actively harmful answer),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (the no-leak suppression the recorded category
  is gated on the same way a job's is),
  [ADR-0015](0015-sql-migration-runner.md) (the additive, forward-only migration).
- **Leaves standing:** [ADR-0128](0128-remote-provision-vm-creation-gaps.md)
  (`terminal=True` on the provision-failure path, and its rejection of failing the
  terminal-state re-entry) and [ADR-0483](0483-non-retryable-category-dead-letters-a-job.md)
  (the category-driven terminal decision). Neither is changed; see §4.

## Context

Two writes record a System failure, and nothing spans them.

`Worker._run_handler` (`jobs/worker.py`) sets `autocommit=True` on the dispatch connection, so
`_record_system_failure`'s `SYSTEMS.update_state(..., FAILED)` commits the moment its transaction
closes. The handler then sets `exc.terminal = True` and re-raises. Only then does the worker
acquire a **separate** pool connection for `queue.fail`, which is the write that sets
`jobs.error_category` — the category's one and only durable home. `systems` had no column for it,
by ADR-0454 §1's deliberate choice.

The window between those two writes is therefore a window in which the reason a System failed
exists nowhere durable. Closing it does not require a process to die: `queue.fail` raises from
inside an `except` block (a pool-acquire timeout is the ADR-0449 failure mode), and
`_claim_loop` catches `Exception` and continues, so the worker survives and the job is simply
never finalized.

Two different losses follow, and the issue describes only the first:

1. **Attempts exhausted** (`attempt >= max_attempts`). The job stays `running` until its lease
   lapses. `repair_abandoned_jobs` (`reconciler/repairs/jobs.py`) then dead-letters it with
   `error_category = 'lease_expired'` and an untouched, empty `failure_context` —
   unconditionally. ADR-0454 §3's derived detail makes that *legible* (the agent is told the job
   was abandoned before recording a reason) but recovers nothing: the System reports
   `lease_expired`, which `RETRYABLE_BY_CATEGORY` calls retryable, for a System that may have
   failed on a non-retryable configuration error.
2. **Attempts remaining** (`attempt < max_attempts`) — **the commoner shape, and not in the
   issue.** The reconciler never sees the job; its sweep requires `attempt >= max_attempts`.
   `queue.dequeue` reclaims the lapsed-lease job instead, and on re-dispatch the handler finds
   the System already in `TERMINAL_SYSTEM_STATES` and early-returns success
   (`handlers/systems.py`, the behaviour ADR-0128 chose on purpose). The job lands
   **`succeeded`**, so ADR-0454 §2's "the newest terminal job did not fail" branch attributes
   nothing at all and the envelope falls back to the flattened `infrastructure_failure` default
   — the exact surface #1550 existed to remove, reached by a different road.

## Decision

### 1. `systems` gains `failure_category`, written in the handler's own transaction

Migration `0083_systems_failure_category.sql` adds a nullable `text` column with a named CHECK
mirroring `ErrorCategory`, matching `runs.failure_category` and `allocations.failure_category`
and registered in `test_migrate.py`'s `CHECK_ENUMS` so it cannot drift from the enum.

`_record_system_failure` takes the provider error's `category` and writes it inside the
`conn.transaction()` it already opens for `SYSTEMS.update_state(..., FAILED)`. The two statements
commit together or not at all. There is no window left, because there is no second write to
reach: the durable answer is on the row whose state change raised the question.

The write is inside the function's existing `try`, so it inherits the best-effort contract that
was already there — a failure to record the reason must never displace the provider error the
caller re-raises. It carries a redundant `state = 'failed'` guard so a category can never land on
a System that is not failed.

### 2. This deliberately reverses ADR-0454 §1, and the cost argument is the thing that changed

ADR-0454 §1 rejected exactly this shape — "a migration plus a write on every failure path, to
cache a value derivable from a row that already exists and is never mutated after the
dead-letter" — and recorded it as rejected **on cost, not on principle**, naming two revisit
triggers: a `systems.list` fix that needs it (§4), and, in Consequences, this issue.

Both have now fired, and the load-bearing premise has been falsified. The value is *not*
reliably derivable: §Context shape 2 is a path on which the job row that would carry it ends
`succeeded` with `error_category` NULL, and shape 1 is a path on which it is overwritten with a
category about the bookkeeping rather than the failure. A cache is optional; a sole durable
record is not, and the column is the latter. The priced costs are met head-on rather than
waved past:

- **The migration.** Additive and forward-only, NULL for every existing row, no backfill. The
  three fallback paths (§3) mean a NULL column degrades to exactly today's behaviour.
- **A write on every failure path.** One `UPDATE` on a row already locked and being updated in
  the same transaction, on the failure path only. Measured against what it replaces — ADR-0454
  §1's own admission that the attribution query is an unindexed sequential scan of an
  append-only `jobs` table — it is the cheaper half of the pair, and ADR-0491's expression index
  (migration 0082) has since made the *read* cheap too, so neither side of the trade is now the
  expensive one.

The alternative that keeps ADR-0454 §1 intact is to put the `queue.fail` write inside the
handler's transaction (the issue's third suggestion). That is rejected in §5.

### 3. The recorded category wins; the failing-job lookup stays as the fallback

`_resolve_failure_verdict` now reads `system.failure_category or failing_job.error_category or
INFRASTRUCTURE_FAILURE`. ADR-0454's whole attribution mechanism — `SYSTEM_FAILING_JOB_KINDS`, the
newest-terminal-job rule, `failing_job_id` as the pointer to the structured
`failure_detail_*` keys — is kept, not replaced, because three real paths still record no
category on the System:

- `repair_stalled_restoring_systems` drives a System to `failed` with no job and no exception.
- Rows that predate migration 0083.
- A non-`CategorizedError` escaping the handler, which dead-letters the job without ever reaching
  `_record_system_failure`.

The two sources agree on every normal path — both derive from the same exception, since
`_failure_category(exc)` returns `exc.category` for a `CategorizedError` — and diverge exactly in
the window this ADR closes. Precedence goes to the System's own record because it is the write
that cannot be separated from the failure it explains.

Two consequences of that ordering are stated rather than left implicit:

- **A recorded category gets its own `detail`, because neither existing string is true beside
  it.** `NO_JOB_SYSTEM_FAILURE_DETAIL` says no reason was recorded; `ABANDONED_JOB_SYSTEM_FAILURE_
  DETAIL` says the original reason was not retained. Both are false in an envelope that reports
  the retained verdict, and the second is exactly what shape 1 produces — a real category beside
  a `lease_expired` job. `RECORDED_SYSTEM_FAILURE_DETAIL` says the one thing that is true: the
  category is on the System, and only the redacted *message* — which lives on the job and nowhere
  else — was lost. The job's own `failure_message` still wins when it has one, so this is a
  fallback, not a replacement.
- **The no-leak rule (ADR-0454 §2a) applies to the recorded category, but drops less.**
  `not_found` and `authorization_denied` are reserved envelope-level meanings for `systems.get`,
  and that is a property of the *category*, not of where it was written; a provider raising
  `NOT_FOUND` reaches `_record_system_failure` on the provision path. A recorded no-leak category
  is therefore not reported — but only the *category* is dropped, and resolution falls through to
  the job, which may explain itself perfectly well. Discarding a citable job because the System's
  column happened to hold a no-leak value would withhold a category, a message, and an id for no
  gain. The converse stays as ADR-0454 set it: a **job's** no-leak category still drops the job
  whole, and the System's own recorded verdict survives that drop.
- **`suggested_next_actions` is unchanged.** ADR-0454 leads with `jobs.wait` whenever a job is
  cited, and on shape 1 that job is a `lease_expired` stub with an empty `failure_context`. That
  is not new — the same list was emitted for the same job before this change — and the job is
  still the audit trail and still carries its own truthful verdict about the bookkeeping. The
  envelope no longer *depends* on the agent following the pointer, because the category and
  `retryable` are now in hand; re-deriving the action list from what the pointer would yield is a
  separate change to an ADR-0454 surface and is not made here.

### 4. `repair_abandoned_jobs` keeps stamping `lease_expired`, and ADR-0128's re-entry stands

The issue's second suggestion — have `repair_abandoned_jobs` preserve an already-set
`error_category` — is rejected as a fix for this, because there is nothing to preserve: the
window is defined by `queue.fail` never having run, so the column is NULL. Where it *is* non-NULL
on a `running` job it is stale, left by an earlier attempt whose requeue cleared
`failure_context` but not the category, and preserving that would trade this bug for a
mis-attribution. The reconciler's statement is also true on its own terms: that job's lease did
expire. With the System carrying its own verdict, a truthful job surface and a truthful System
surface no longer have to be the same sentence.

ADR-0128's "Alternatives considered" already rejected making the terminal-state re-entry return
failure — it closes the masking only after burning every retry, and the final `failure_context`
is the re-entry message rather than the original reason. That rejection is untouched: shape 2 is
fixed by the System keeping its own record, not by changing what the re-entry returns, so a
re-dispatched job still ends `succeeded` and the System still reports the real reason.

ADR-0483 changed `_is_terminal`, which runs inside the same `except` block *after* this window
opens. It cannot help and is not amended.

### 5. Rejected: reuse the handler's connection for `queue.fail`, or open a transaction across both

The issue's third suggestion makes the two writes atomic from the other side. It is rejected on
architecture, not on effort:

- It inverts ADR-0018 decision 7. The worker deliberately holds **no** transaction across the
  handler, because a handler runs 30+ minutes; the handler's connection is autocommit precisely
  so its own steps commit as they go. Making the failure path transactional means the queue write
  becomes contingent on a connection the handler may have poisoned — and `_run_handler`'s comment
  records that finalizing on a fresh connection is the reason it survives a poisoned one.
- It would put a `jobs`-table write inside a domain handler's transaction, making the handler a
  writer of queue state. Every handler would then have to be audited for it.
- It closes only the half of the problem the issue saw. `queue.fail` raising is one way to lose
  the window; the worker process being killed between the handler's commit and *any* subsequent
  statement is the other, and no arrangement of a second write closes that one. Only writing the
  reason in the transaction that establishes the failure does.

## Consequences

- A System failed by a worker that died — or whose `queue.fail` never landed — reports its real
  category and its real `retryable` verdict. Both loss shapes are pinned by tests that drive the
  real handler against a real database and simply do not call `queue.fail`, which is what the
  worker does when it dies there.
- **`systems.list` now reports the truthful category too**, closing most of the gap ADR-0454 §4
  disclosed and pinned. The column is on the row the list path already reads, so this costs no
  query and needs none of the set-based lateral join that ADR-0454's follow-up scoped. The gap is
  narrowed rather than closed: a System whose failure predates the column, or whose category is
  only on its job, still lists as `infrastructure_failure`, because resolving *that* still needs
  the per-row lookup ADR-0454 §4 kept off the list path.
- `detail` stays get-only. It is still gated on `failure_attributed` — the flag says the caller
  ran the lookup, and every derived reason is a positive claim about a fact that was checked, so
  the list path stays silent on the reason while now being truthful about the category.
- One new logging branch is reachable from the list path (a recorded no-leak category), where
  ADR-0454 could state that no logging branch was. It fires only for a suppressed category, which
  no normal failure path produces.
- No new query, no new index, no config setting, no dependency. No MCP tool schema, RBAC, or
  exposure change: the tool arguments are untouched and only the failure envelope's category
  differs. `System.failure_category` is a new field on the domain record, serialized nowhere the
  envelope does not already control.
- Category and `detail` can now come from different jobs, where ADR-0454 always took both from
  one row. It needs a genuine race on the serialized system-lifecycle kinds: job B fails the
  System (recording category B) while job A's provider call is in flight, so A hits the
  `IllegalTransition` arm and writes no category but dead-letters newer, winning the attribution.
  Disclosed as a residual of the same kind ADR-0454 already disclosed — the correlation is an
  ordering argument over `created_at`, not an invariant the code enforces.
- The same window still exists for **Runs**: `_compensate_run_failure` writes
  `runs.failure_category` on the worker's post-handler connection alongside `queue.fail`, so a
  worker that dies in the window leaves a Run without its category too. That is a different
  writer with a different owner and is deliberately not folded in here; it is disclosed rather
  than fixed.

## Considered & rejected

- **Reuse the handler's connection for `queue.fail`, or span both writes in one transaction.**
  Inverts ADR-0018 decision 7, makes handlers writers of queue state, and still cannot survive
  the worker being killed (§5).
- **Have `repair_abandoned_jobs` preserve an already-set `error_category`.** Nothing to preserve
  — the window is defined by `queue.fail` never running — and where a category *is* present on a
  `running` job it is a stale one from a prior attempt (§4).
- **Make the terminal-state re-entry return failure instead of success.** Already rejected by
  ADR-0128 as strictly worse (burns every retry, and the surviving reason is the re-entry
  message). Fixing shape 2 by the System's own record leaves that rejection intact (§4).
- **Record the redacted failure *message* on `systems` as well.** Redaction is the worker's job
  (`_failure_context` runs the `Redactor` over the exception with the secret registry), and
  `restore_handler` has no registry to run it with; pushing that boundary into the handlers to
  duplicate a structured surface `jobs` already owns is the duplication ADR-0454 §3 declined.
  The category is the field `retryable` is derived from and is the whole of what #1562 loses
  durably; the message's loss is already made legible by ADR-0454 §3's derived detail.
- **Backfill the column from the historical `jobs` rows.** A migration doing the ADR-0454
  correlation once, at deploy time, over an append-only table — for rows an agent has already
  read and acted on. The NULL fallback (§3) gives those rows exactly today's answer.
