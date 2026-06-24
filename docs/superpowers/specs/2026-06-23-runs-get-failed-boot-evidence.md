# Spec — `runs.get`: distinguish a failed boot attempt from a never-attempted boot (#750)

- **Issue:** #750 (part of #746, Part 2 black-box review follow-up)
- **ADR:** [ADR-0230](../../adr/0230-runs-get-failed-boot-evidence.md)
- **Status:** Draft
- **Date:** 2026-06-23

## Problem

After a boot job terminally fails (e.g. `readiness_failure`), `runs.get` still reports
`data.steps.boot:"pending"` — indistinguishable from "boot never attempted", and ambiguous
about whether a retry is in flight. An agent reading the envelope cannot tell a Run that has
*failed to boot* from one that simply *has not booted yet*, so it cannot decide whether to
retry, re-bind, or surface the failure to the operator.

### Why the ledger says "pending"

Verified against `main` (`f006a879`):

- The `run_steps` ledger has only `running` / `succeeded` (`db/idempotency.py`; DB CHECK
  `db/schema/0043_run_steps_state_check.sql`) — there is no `failed` state.
- On boot failure the step row is **deleted**: `jobs/handlers/runs_boot.py` →
  `abandon_run_step_best_effort` → `abandon_run_step` issues
  `DELETE FROM run_steps WHERE state='running'` (`db/idempotency.py`). This deletion is
  deliberate: a clean in-place retry recycles the step (`services/runs/steps.py`, ADR-0185).
- `runs.get` renders a missing row as `"pending"` (`services/runs/steps.py` `step_progress`;
  envelope at `mcp/tools/lifecycle/runs/common.py` `envelope_for_run`). A failed boot therefore
  reverts to `pending`.

### Where the evidence already lives

The boot job itself is **not** deleted. It is enqueued with a deterministic
`dedup_key = f"{run_id}:boot"` (`mcp/tools/lifecycle/runs/steps.py` `_enqueue_step`). On a
terminal failure the worker marks that job `failed` and records its `error_category`
(`jobs/worker.py`). On a retry the *same* row is recycled in place — reset to `queued`,
`error_category` cleared (`jobs/queue.py` `enqueue(..., retry_terminal_failed=True)`). So at
any instant there is at most one boot job per Run, reachable by `dedup_key`, and its `state` +
`error_category` already encode the three-way distinction the issue asks for.

## Goal

On `runs.get`, when the boot step reads as `pending`, surface evidence of the last boot
*attempt* (its terminal status + `error_category`) so an agent can distinguish:

1. **Never attempted** — no boot job exists (or it is still `queued`/`running`): keep
   `steps.boot:"pending"`, no boot-attempt evidence.
2. **Terminally failed** — the boot job is `failed`: keep `steps.boot:"pending"` (the ledger
   row is gone by design) **and** carry the failed attempt's `{job_id, status, error_category}`.

The `run_steps` retry design (ADR-0185) is left untouched — this is an additive read.

## Approach (recommended in the issue, settled by the orchestrator)

Additive, no ledger change. On the `runs.get` read path, when the boot step is not yet
`succeeded`, look up the boot job by its deterministic `dedup_key` and, **only when it is in a
terminal `failed` state**, attach a small `boot_readiness` object to the envelope `data`:

```jsonc
"boot_readiness": {
  "job_id": "<uuid>",
  "status": "failed",
  "error_category": "<ErrorCategory value, e.g. readiness_failure>"
}
```

- Surfaced only when the boot job's `state` is `failed`. A `queued`/`running` boot job (a retry
  in flight, or a first attempt mid-run) does **not** emit `boot_readiness` — it is still
  "pending, nothing decided", which the existing `steps.boot:"pending"` already conveys, and
  emitting an in-flight `error_category` would be misleading (there is none).
- Surfaced only when the boot step is not `succeeded`. Once boot has succeeded the ledger row is
  authoritative and there is no failed attempt to report.
- `error_category` may be `None` on a `failed` job that recorded no category; `boot_readiness`
  then carries `"error_category": null`. `status` is always present (`"failed"`).

### Why `boot_readiness` lives in `data`, not `refs`

`refs` is for object-store artifact keys (kernel, debuginfo, console). The boot-attempt evidence
is structured status metadata, not an artifact pointer, so it belongs in `data` — the same slot
that already carries `steps`, `required_cmdline`, and `expected_boot_failure`.

## Acceptance criteria

A reviewer can check each of these against the test suite:

1. After a terminally-failed boot (boot job `failed` with `error_category=readiness_failure`,
   boot ledger row deleted), `runs.get` on the `SUCCEEDED` Run returns
   `data.steps.boot == "pending"` **and** `data.boot_readiness ==
   {job_id, status:"failed", error_category:"readiness_failure"}`.
2. Never-attempted boot (no boot job; install may or may not be done): `data.steps.boot ==
   "pending"` and **no** `boot_readiness` key.
3. Boot retry in flight (boot job exists, `state` `queued` or `running`): `data.steps.boot ==
   "pending"` and **no** `boot_readiness` key.
4. Successful boot (boot ledger row `succeeded`): no `boot_readiness` key — the success path is
   authoritative and unchanged.
5. A `failed` boot job whose `error_category` is `None` yields
   `boot_readiness.error_category == null` (still surfaced; `status:"failed"` present).
6. The fielded `runs.get` output schema includes the new optional `boot_readiness` field; the
   committed snapshot is regenerated.

## Non-goals / out of scope

- **No `failed` state added to the `run_steps` ledger.** That collides with the recycle-on-retry
  design of ADR-0185 and is explicitly out of scope (recorded as a rejected alternative in
  ADR-0230).
- No DB migration — additive read only.
- `runs.list` is not changed: it does not load per-Run step/job state and the single-run
  evidence chase does not justify a per-Run boot-job query on the list path.
- No new `error_category` values; the boot job already carries the most specific category.

## Edge cases

- **Boot job missing** (never enqueued): `get_by_dedup_key` returns `None` → no evidence. ✅ (2)
- **Boot job `queued`/`running`** (retry in flight or first attempt): not `failed` → no
  evidence. ✅ (3)
- **Boot ledger row present and `running`** (attempt actively in flight, not yet abandoned): boot
  step reads `running`, not `pending`; we still suppress `boot_readiness` because the step is not
  in a failed-evidence state. The lookup is gated on `boot != "succeeded"`, but a concurrent
  `running` ledger row with a stale `failed` job is not reachable in practice (the recycle resets
  the job to `queued` before re-claiming the step). Tested at the `step_progress` boundary.
- **`error_category` absent on a `failed` job**: surfaced as `null`. ✅ (5)
- **Project scoping**: the Run is already project-scoped before the envelope is built; the boot
  job is looked up by `dedup_key` derived from `run_id` for that same Run, so no cross-project
  leak is possible. The job id surfaced is for the caller's own Run.
