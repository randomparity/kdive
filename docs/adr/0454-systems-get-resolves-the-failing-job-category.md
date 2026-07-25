# ADR 0454 — `systems.get` resolves the failing job's category instead of assuming one

- **Status:** Accepted
- **Date:** 2026-07-25
- **Depends on:** [ADR-0118](0118-wait-on-resource-mechanisms.md) (the derived `retryable` field —
  a pure function of the failure category),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (the no-leak `detail` seam),
  [ADR-0141](0141-failed-run-reason-surfacing.md) (the failing-job attribution shape `runs.get`
  already uses).
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
(`jobs/queue.py`, `reconciler/repairs/systems.py`). `get_system` performs one additional indexed
lookup, only when the System is `failed`, and passes the job into the envelope.

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

Matching on `state = 'failed'` is matching exactly the row that carries the answer, not a
convenience: `queue.fail` writes `error_category` **only** on the dead-letter branch — a requeue
clears `failure_context` and leaves `error_category` NULL — and both job-backed writers set
`exc.terminal = True` immediately after recording the System failure, precisely so a retry cannot
mask it. The correlated job therefore dead-letters on its first attempt with its category intact.

### 3. The no-job default stays, and is now load-bearing rather than universal

`failing_job.error_category or INFRASTRUCTURE_FAILURE` keeps the existing default for the case it
was actually right for: the reconciler orphan, where a `restoring` System whose restore job can
never run again is resolved to `failed` with no job to attribute it to. That path is covered by its
own test, so the default is a tested branch rather than dead code.

A failed System is never surfaced as a bare category. With a job, `detail` is the job's
`failure_context["failure_message"]` and `data.failing_job_id` points at `jobs.get` for the
structured `failure_detail_*` keys — which are deliberately **not** duplicated onto the System, so
there is one place a structured failure reason lives. Without a job, `detail` is a fixed
resource-free string. Both are gated on the ADR-0123 no-leak rule the same way
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

## Consequences

- An agent polling `systems.get` on a `failed` System now reads the same category, `retryable`
  verdict, and reason its provisioning job reported. The #1550 live case (`configuration_error`,
  `retryable: false`) is reported truthfully.
- `systems.get` issues one extra query on the `failed` path only. Every other state, and the whole
  of `systems.list`, is unchanged.
- Correlation is a query, not a stored foreign key, so it is a best-effort attribution with three
  recorded residuals: a System is briefly `failed` before the worker's `queue.fail` write lands
  (separate connections, milliseconds) and reads as the default in that window; a job row removed
  by retention takes its category with it; and if a second matching-kind job for the same System
  were ever to dead-letter after the one that failed it, the newer row would win. The last is not
  reachable today — `SystemState.FAILED` is terminal, with no outbound transitions — but it is a
  property of the query, not an invariant the code enforces.
- `systems.list` keeps the flattened category (§4).
- No schema, no migration, no config, no new dependency. No MCP tool schema, RBAC, or exposure
  change: the tool's arguments are untouched and only the failure envelope's contents differ.

## Considered & rejected

- **Denormalise `failure_category` onto `systems`.** A migration and a write on every failure path
  to cache a derivable value (§1). Revisit only if `systems.list` needs it.
- **Take the newest failed job of any kind.** Simpler query, wrong answer: a routine failed
  `check_ssh_reachable` would be reported as the reason the System is `failed` (§2).
- **Fold the lookup into `system_envelope` so `systems.list` benefits too.** A per-row N+1 on the
  list path (§4).
- **Copy the `failure_detail_*` keys onto the System envelope.** Duplicates a structured surface
  that `jobs.get` already owns; `failing_job_id` is the pointer instead (§3).
