# ADR 0454 — `systems.get` resolves the failing job's category instead of assuming one

- **Status:** Accepted
- **Date:** 2026-07-25
- **Depends on:** [ADR-0118](0118-wait-on-resource-mechanisms.md) (the derived `retryable` field —
  a pure function of the failure category),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (the no-leak `detail` seam),
  [ADR-0141](0141-failed-run-reason-surfacing.md) (the failing-job attribution shape `runs.get`
  already uses),
  [ADR-0149](0149-failed-system-provision-retry-ergonomics.md) (the sibling `systems.provision` surface that
  already resolves a failed System's reason, by `dedup_key` — see §5).
- **Leaves standing:** [ADR-0445](0445-reconcile-checksum-mismatch-error-category.md). That ADR
  made the *job* surface report a truthful category; this one stops the *System* surface from
  discarding it. Neither category assignment changes.
- **Spec:** [`../specs/2026-07-25-systems-get-failure-category-1550-design.md`](../specs/2026-07-25-systems-get-failure-category-1550-design.md)

## Context

`system_envelope` reported `ErrorCategory.INFRASTRUCTURE_FAILURE` for every `SystemState.FAILED`,
unconditionally and with no `detail`. `_RETRYABLE_BY_CATEGORY` (ADR-0118) derives the
agent-visible `retryable` boolean from the category, and `INFRASTRUCTURE_FAILURE` is retryable —
so a System failed by a non-retryable **configuration** error was reported to an agent as a
retryable infrastructure fault with nothing actionable attached.

This was observed live during the #1502 campaign's isolation re-proof: a System bound to
investigation B was provisioned against a checksum owned by investigation A. `jobs.get` reported
`configuration_error` / `retryable: false` with a message naming the fix; `systems.get` on the
System that job left behind reported `infrastructure_failure` with no detail. Polling the System
rather than the job is a natural thing for an agent to do — `systems.provision` returns a job whose
`data.system_id` names the durable object — so the truthful category ADR-0445 established on the
job surface was silently undone for anything watching Systems.

`runs` and `allocations` both already resolve this correctly, each reading a per-row column
(`X.failure_category or INFRASTRUCTURE_FAILURE`). `systems` is the odd one out because its table
has no such column.

## Decision

### 1. Resolve the failing job; do not add a column

`systems` gains no `failure_category` and no `failing_job_id`, and this change ships **no
migration**. The failing job is already queryable: `jobs.error_category` exists, the system-scoped
kinds carry `payload.system_id`, and `payload->>'system_id'` is an established join key
(`jobs/queue.py`, `reconciler/repairs/systems.py`). `get_system` performs one additional query,
only when the System is `failed`, and passes the job into the envelope.

That query is **not** index-backed and this ADR does not claim it is: `jobs` carries no index
beyond its primary key and the `dedup_key` unique constraint, so the lookup is a sequential scan
plus a sort over a table that is effectively append-only (the only `DELETE FROM jobs` in the tree
is a single `dedup_key` delete in the GC). `latest_succeeded_job_for_system` already has exactly
this shape, so it is an existing cost rather than a new class of one, and it is paid only on the
`failed` branch of a single-row read. The partial index that would fix it
(`(payload->>'system_id'), created_at DESC WHERE state = 'failed'`) needs a migration, which this
change deliberately does not carry; it is filed as a follow-up. A reader weighing the `systems.list`
follow-up (§4) should price it against a scan, not against a keyed lookup.

The alternative — denormalising the category onto `systems` at the moment of failure — would need a
migration plus a write on every failure path, to cache a value derivable from a row that already
exists and is never mutated after the dead-letter. It is recorded as rejected on cost, not on
principle: if a future `systems.list` fix needs it (§4), that is the shape to revisit.

### 2. Correlation is by job **kind**, because "the newest failed job" is not the same question

A System accumulates failed jobs of kinds that never touch its state — a failed
`check_ssh_reachable` on a healthy System is routine. Taking the newest failed job of *any* kind
would answer a different question and would confidently mis-attribute.

A full sweep of `src/` finds exactly three `SystemState.FAILED` writers: `provision` /
`reprovision` (`_execute_system_lifecycle_call`), `restore` (`restore_handler`), and the
reconciler's `repair_stalled_restoring_systems`, which has no job at all. The first three kinds
become `SYSTEM_FAILING_JOB_KINDS`, declared beside the existing `JobKind` sets in
`domain/operations/jobs.py` so the set sits with the enum it constrains rather than in the read
path that consumes it.

`error_category` is written **only** on `queue.fail`'s dead-letter branch — a requeue clears
`failure_context` and leaves the category NULL — and both job-backed writers set
`exc.terminal = True` immediately after recording the System failure, precisely so a retry cannot
mask it. A `failed` row is therefore exactly the row that carries an answer.

The query nevertheless does **not** filter to `failed` and take the newest match. It takes the
newest job in *any* terminal state (`succeeded`/`failed`/`canceled`) and attributes it only if
that state is `failed`. Filtering first would skip *over* a newer terminal job to reach a stale
older failure, and that is reachable: `restore` is contributor-cancelable, and cancelling one
satisfies `repair_stalled_restoring_systems`' "no restore job queued/running" predicate, so the
System is driven to `failed` with nothing to attribute — while an older failed `provision` row can
survive from a lifecycle the System went on to recover from (a non-`CategorizedError` escaping the
handler dead-letters the job without touching System state). Reporting that older row would be
strictly worse than the default it replaces, because it carries a confident category, a confident
message, and a `failing_job_id`. "The last system-lifecycle job to finish did not fail, so I have
no attribution" is the honest answer, and it is the default's second real branch.

### 2a. A no-leak category is not forwarded as the System's own verdict

`not_found` and `authorization_denied` are reserved *envelope-level* meanings here (ADR-0097):
`systems.get` itself returns `not_found` for a System that is absent or invisible. Forwarding a
job's `not_found` onto a System the caller just read successfully would tell an agent the object
is gone in the same envelope that carries the object's whole record — and `error_category` is the
one field this whole change exists to make trustworthy.

It is reachable: `restore_handler` binds its snapshotter *before* its `try`, and the resolver
raises `NOT_FOUND` when the System's Resource row is missing. That escape never reaches
`_record_system_failure`, so the job dead-letters `not_found` while the System stays `restoring`,
and the reconciler then resolves it to `failed`.

Such a job explains nothing the System surface may repeat, so it is dropped whole: the category
degrades to the default and `failing_job_id` is withheld (ADR-0123 already withheld the `detail`;
forwarding the bare category was the one misleading thing left). This subsumes the earlier gate,
which asked only "may I show the job's extras?" and never "is this category coherent as the
System's own verdict?".

The reason on this path is its **own** string, not the "no job recorded a reason" one: a job did
record a reason here, so claiming none exists would be a second falsehood and would steer an agent
away from the one place the verdict survives. `jobs.list` (which takes a `system_id` filter) leads
the next actions instead of `jobs.get`, so "the job stays readable" is something the envelope says
to the agent rather than something this ADR says to its reader.

### 3. The reason is derived from the resolved detail, not from the presence of a job

`failing_job.error_category or INFRASTRUCTURE_FAILURE` keeps the existing default for the case it
was right for: a `failed` System with no attributable job. That path is covered by its own test, so
the default is a tested branch rather than dead code.

The `detail` fallback, however, is deliberately **not** keyed on `failing_job is None`, because a
job with an empty `failure_context` is exactly as reasonless as no job — and is the likelier shape.
`repair_abandoned_jobs` dead-letters any zombie job (`state = running`, lapsed lease, attempts
exhausted — no kind filter) by writing `error_category = 'lease_expired'` while never touching
`failure_context`, which is `'{}'` by table default or was reset to `'{}'` by a prior requeue.
`repair_stalled_restoring_systems` then resolves the `restoring` System to `failed`; its candidate
predicate ("no restore job in `queued`/`running`") is satisfied *precisely because* that sweep just
dead-lettered it, and its own docstring says it runs after it. So the case §3 would otherwise call
"the orphan with no job" usually **does** have a matching `failed` row, takes the job branch, and
would have surfaced a bare `lease_expired` with `detail: null` — reintroducing the bare-category
surface #1550 exists to remove, on the reconciler's normal path.

Gating on the resolved reason instead means: the job's `failure_message` when it has one, else a
fixed resource-free string derived from the category. `lease_expired` earns its own string, since
it is the one category reachable *without* a handler having written a reason and its bare form
reads as a verdict on the System rather than on the bookkeeping; every other category falls back to
the generic constant. `data.failing_job_id` still points at `jobs.get` for the structured
`failure_detail_*` keys, which are deliberately **not** duplicated onto the System so that one
place owns a structured failure reason. All of it is gated on the ADR-0123 no-leak rule the same way
`runs/common.py::_failed_envelope` gates its own job-derived surface, because `data` extras bypass
`ToolResponse.failure`'s built-in suppression.

### 4. Scope is `systems.get`; the `systems.list` gap is disclosed, not silently left

`system_envelope` is shared by the get path and the list path, so resolving the failing job inside
it would put a per-row query on `systems.list`. The job is therefore resolved by `get_system` and
threaded in as an optional argument, matching the existing get-only convention in the same file for
`active_run` and `active_debug_session_ids`, both already documented there as N+1 on the list path.

**The consequence is that an agent listing Systems still sees every `failed` System as
`infrastructure_failure`.** Closing it needs a set-based lateral join rather than the per-row lookup
this ADR adds, which is a different change; it is filed as a follow-up and pinned by a test here, so
the gap is an asserted fact rather than an assumption that can rot.

### 5. The failure envelope names a recovery, since `retryable` cannot name one

`SystemState.FAILED` is terminal — its outbound transition set is empty — so no System-scoped tool
can act on a `failed` System whatever `retryable` says. The envelope previously carried no
`suggested_next_actions` at all, which is the other half of #1550's "nothing actionable attached":
this change attaches the reason, and without a next action the reason still terminates in a dead
end. It now names `allocations.release` / `allocations.request` — the same recovery
`_failed_system_retry_failure` (ADR-0149) already gives an agent that calls `systems.provision` on
this System. The list is static and unfiltered, matching the success path's list in the same
function rather than threading a `RequestContext` into a pure renderer.

ADR-0149's surface is not superseded and not extended. It correlates by the deterministic unique
`dedup_key` `"{allocation_id}:provision"` — genuinely index-backed, unlike §1's scan — but that key
exists only for provision, is scoped to the Allocation rather than the System, and so cannot
attribute a `reprovision` or `restore` failure at all. `systems.get` is authoritative for *why a
System is `failed`* across all three kinds; `systems.provision` remains authoritative for *what to
do about it when you tried to re-provision*, and the two now agree on the recovery even where their
categories differ by design (ADR-0149 answers "can I re-provision?" — always
`configuration_error`, never retryable — while `systems.get` answers "what failed?").

## Consequences

- An agent polling `systems.get` on a `failed` System now reads the same category, `retryable`
  verdict, and reason its provisioning job reported. The #1550 live case (`configuration_error`,
  `retryable: false`) is reported truthfully.
- `systems.get` issues one extra query on the `failed` path only — a sequential scan of `jobs`
  (§1). Every other state, and the whole of `systems.list`, is unchanged.
- A `failed` System now carries `suggested_next_actions` where it carried none (§5). This is the
  only behavior change that also reaches `systems.list`, since it needs no query — and it is kept
  to that by an explicit `failure_attributed` flag. Every derived `detail` is a positive claim
  about a fact that was *checked*, and the list path never checks, so a bare `failing_job=None`
  is not allowed to mean "looked and found none" there: the list path keeps its pre-existing
  `detail=None` silence. Without that flag `systems.list` would have told an agent that no job
  recorded a reason for **every** `failed` System, including ones whose job recorded an excellent
  one — a confident falsehood, and one that gives an agent a positive reason not to call
  `systems.get`. Both halves are pinned by tests.
- **The two writes this attribution spans are not atomic, and the failure mode is durable, not
  transient.** `_run_handler` runs the handler on an autocommit connection, so
  `_record_system_failure`'s `SYSTEMS.update_state(..., FAILED)` commits at once; `queue.fail` then
  runs on a *separate* pool connection. If the worker dies in between, or `queue.fail` itself
  raises (a pool-acquire timeout — the ADR-0449 failure mode — raised from inside an `except`
  block, so it escapes), the job never reaches `failed` by the handler's hand. It stays `running`
  until its lease lapses, and `repair_abandoned_jobs` then stamps `lease_expired` over it with an
  empty `failure_context`, unconditionally: the truthful category and its actionable message are
  **permanently** gone. §3's derived detail makes that outcome legible rather than silent — the
  agent is told the job was abandoned before it recorded a reason, instead of reading a confident
  `lease_expired` with no explanation — but it does not recover the lost reason. Closing it needs
  the System failure and the job failure to be recorded together; that is a worker-plane change,
  filed as a follow-up.
- Correlation is a query, not a stored foreign key, so it is best-effort in two further ways: a
  `systems.get` landing between the System's commit and `queue.fail` reads the no-job default for
  that window; and the correlation is *positional* — the newest terminal system-lifecycle job —
  rather than a recorded link. §2's terminal-state rule closes the reachable mis-attribution (a
  newer cancel or success no longer lets a stale older failure through), but it remains an
  ordering argument over `created_at`, not an invariant the code enforces; a recorded
  `failing_job_id` on `systems` is what would make it one, and that needs the migration §1
  declines. There is no jobs retention sweep, so a row's disappearance is not among the
  residuals.
- `systems.list` keeps the flattened category (§4).
- Each flattening to the default category is logged at INFO (a dropped no-leak verdict, a
  NULL-`error_category` row). #1550 survived because the flattening was silent — it was found by a
  human reading a live envelope — so shipping the fix with new silent flattening paths would
  reproduce the blind spot. The no-job case is not logged: it is the expected shape, and the list
  path never reaches the logging branches, so there is no per-row cost.
- No schema, no migration, no config, no new dependency. No MCP tool schema, RBAC, or exposure
  change: the tool's arguments are untouched and only the failure envelope's contents differ.
  `jobs.get`/`jobs.list` are `_VIEWER`, the same grant `systems.get` requires, so the added
  breadcrumbs point at nothing the caller could not already reach.

## Considered & rejected

- **Denormalise `failure_category` onto `systems`.** A migration and a write on every failure path
  to cache a derivable value (§1). Revisit only if `systems.list` needs it.
- **Take the newest failed job of any kind.** Simpler query, wrong answer: a routine failed
  `check_ssh_reachable` would be reported as the reason the System is `failed` (§2).
- **Fold the lookup into `system_envelope` so `systems.list` benefits too.** A per-row N+1 on the
  list path (§4).
- **Copy the `failure_detail_*` keys onto the System envelope.** Duplicates a structured surface
  that `jobs.get` already owns; `failing_job_id` is the pointer instead (§3).
- **Extend ADR-0149's `dedup_key` correlation instead of a payload match.** Genuinely
  index-backed, but the key is `"{allocation_id}:provision"` — provision-only and
  Allocation-scoped, so it cannot attribute a `reprovision` or `restore` failure at all (§5).
- **Add the partial index the scan wants, in this change.** It needs a migration, which #1550 was
  scoped to avoid; recorded as a follow-up so the cost is priced rather than hidden (§1).
- **Suppress `lease_expired` and report the default category instead.** Rejected as *less*
  truthful: the job row genuinely says the lease expired. The derived detail explains what that
  means for the System without overwriting what the row records (§3). This is the opposite call
  from §2a's, and deliberately so: `lease_expired` is an accurate statement about the System's
  fate, while `not_found` is an envelope-level claim about the System's *existence* that the read
  path has already disproved.
