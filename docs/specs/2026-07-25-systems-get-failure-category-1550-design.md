# `systems.get` reports the failing job's real error category (#1550)

- **Issue:** [#1550](https://github.com/randomparity/kdive/issues/1550)
- **ADR:** [ADR-0454](../adr/0454-systems-get-resolves-the-failing-job-category.md)
- **Date:** 2026-07-25

## Problem

`system_envelope` hard-codes `ErrorCategory.INFRASTRUCTURE_FAILURE` for any
`SystemState.FAILED`:

```python
if system.state is SystemState.FAILED:
    return ToolResponse.failure(
        str(system.id),
        ErrorCategory.INFRASTRUCTURE_FAILURE,
        data={"current_status": system.state.value, **data},
    )
```

`_RETRYABLE_BY_CATEGORY` (`src/kdive/mcp/responses.py`) derives the agent-visible `retryable`
boolean from the category, and `INFRASTRUCTURE_FAILURE` is retryable. So a System failed by a
**configuration** mistake — the case #1523 / ADR-0445 just reconciled on the job surface — is
reported to an agent polling `systems.get` as a retryable infrastructure fault, with no detail
saying what to change. Polling the System is natural: `systems.provision` returns a job whose
`data.system_id` names the durable object.

Both peer read paths already do the right thing with a per-row column:

- `src/kdive/mcp/tools/lifecycle/runs/common.py` — `run.failure_category or INFRASTRUCTURE_FAILURE`
- `src/kdive/mcp/tools/lifecycle/allocations/common.py` — `alloc.failure_category or INFRASTRUCTURE_FAILURE`

`systems` has no such column, which is why it is the odd one out.

## Constraints discovered

1. **No column, and none is being added.** `systems` (`0001_init.sql`) has no `failure_category`
   or `failing_job_id`, and no later migration adds one. This change adds **no migration**.
2. **The failing job is queryable today.** `jobs.error_category` exists; the system-scoped kinds
   carry `payload.system_id`; and `payload->>'system_id'` is already an established join key
   (`jobs/queue.py`, `reconciler/repairs/systems.py`).
3. **There are exactly three `SystemState.FAILED` writers** (full sweep of `src/`):
   - `jobs/handlers/systems.py` `_execute_system_lifecycle_call` — `provision` / `reprovision`
   - `jobs/handlers/systems.py` `restore_handler` — `restore`
   - `reconciler/repairs/systems.py` `repair_stalled_restoring_systems` — **no job**
4. **Both job-backed writers force `exc.terminal = True`** immediately after recording the System
   failure. `queue.fail` writes `error_category` only on the dead-letter branch (a requeue clears
   `failure_context` and leaves `error_category` NULL), so the correlated job reliably lands in
   `state = 'failed'` *with* its category. Matching on `state = 'failed'` is therefore matching
   exactly the row that carries the answer.
5. **`system_envelope` is shared** by `get_system` (one row) and `list_systems` (N rows). Folding a
   job lookup into the shared body would be a per-row N+1 on the list path.

## Design

### Resolve, do not store

`get_system` resolves the System's failing job and passes it to `system_envelope`; the envelope
reports `failing_job.error_category or INFRASTRUCTURE_FAILURE`. This reaches the same end state as
the `runs`/`allocations` pattern without a schema change.

New `JobKind` set, beside the existing sets in `domain/operations/jobs.py`:

```python
SYSTEM_FAILING_JOB_KINDS = frozenset({JobKind.PROVISION, JobKind.REPROVISION, JobKind.RESTORE})
```

These are precisely the kinds whose handlers write `SystemState.FAILED`. Restricting to them is
what stops an unrelated failed `check_ssh_reachable` (or any other system-scoped kind) from being
mis-read as the reason a System is `failed`.

New queue helper, mirroring `latest_succeeded_job_for_system`:

```python
async def latest_failed_job_for_system(conn, system_id) -> Job | None
```

`state = 'failed'` AND `kind = ANY(SYSTEM_FAILING_JOB_KINDS)` AND `payload->>'system_id' = %s`,
newest first by `(created_at, id)`.

### Envelope surface

`system_envelope` gains an optional `failing_job: Job | None = None`. On the `FAILED` branch it
builds the failure envelope with:

- `error_category` — the job's category, else `INFRASTRUCTURE_FAILURE`.
- `detail` — the job's `failure_context["failure_message"]` when it has one, else a fixed
  resource-free string derived from the category. Keyed on the **resolved reason**, not on
  `failing_job is None`: `repair_abandoned_jobs` dead-letters a zombie job with `lease_expired` and
  an untouched (empty) `failure_context`, and the System-failing reconciler repair runs after it,
  so a job-with-no-message is the likelier shape and gating on job presence would leave that path a
  bare category (ADR-0454 §3).
- `data.failing_job_id` — so an agent can jump to `jobs.get` for the structured
  `failure_detail_*` keys, which stay there rather than being duplicated onto the System.
- `suggested_next_actions` — `allocations.release` / `allocations.request`. `SystemState.FAILED`
  is terminal, so `retryable` names no System-scoped tool to act with; these are the recovery
  ADR-0149 already gives an agent that calls `systems.provision` on a failed System (ADR-0454 §5).

The job-derived surface (`detail`, `failing_job_id`) is suppressed for a no-leak category
(ADR-0123), gated on `suppressed_detail(category, None) is not None` — the same gate
`runs/common.py::_failed_envelope` uses, because `data` extras bypass `ToolResponse.failure`'s own
suppression.

### Scope: `systems.get` only

`list_systems` keeps passing no `failing_job`, so it keeps the flattened
`infrastructure_failure`. This matches the existing get-only convention in the same file for
`active_run` and `active_debug_session_ids`, both documented as N+1 on the list path. The gap is
disclosed in ADR-0454 §4 and filed as a follow-up rather than left silent.

## Test plan (TDD)

Unit, over `system_envelope` (no DB):

1. FAILED + a job with `error_category=configuration_error` → `error_category` is
   `configuration_error`, `retryable` is `False`, `detail` is the job's message,
   `data.failing_job_id` is the job id. **This is the regression test for #1550.**
2. FAILED + `failing_job=None` → `infrastructure_failure`, a non-empty detail, no
   `failing_job_id`.
3. FAILED + a job whose `error_category` is NULL → `infrastructure_failure`.
4. FAILED + a job on a no-leak category → no `failing_job_id`, detail is the seam constant.
5. Non-FAILED + `failing_job` supplied → still a success envelope (the argument is inert).
6. FAILED + a job with an empty `failure_context` → a derived detail, not `None`.
7. FAILED + a `lease_expired` job with an empty `failure_context` → the abandoned-job detail.
8. The failure envelope names the recovery actions, and they are registered tools.

Integration, over the helper and `get_system` (DB):

6. `latest_failed_job_for_system` returns the newest failed provision job for the System.
7. It ignores a failed job of an unrelated kind (`check_ssh_reachable`).
8. It ignores a failed job belonging to a different System.
9. It ignores a non-`failed` job of a matching kind.
10. `get_system` on a FAILED System end-to-end reports the provision job's category.
11. `list_systems` on the same FAILED System still reports `infrastructure_failure` — pinning the
    disclosed scope so the gap is a tested fact rather than an assumption.
12. The real reconciler sequence end-to-end: enqueue a `restore` job, make it a zombie, run
    `repair_abandoned_jobs` then `repair_stalled_restoring_systems`, and assert `systems.get`
    reports `lease_expired` with the abandoned-job detail. Driven through the actual sweeps so the
    shape cannot drift from them.
