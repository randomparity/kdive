# ADR 0230 — Surface a failed boot attempt as `data.boot_readiness` on `runs.get`

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0185](0185-retry-terminal-failed-step.md) (the recycle-on-retry
  step design this read works *around* without changing),
  [ADR-0179](0179-run-state-step-progress-semantics.md) (the `run_steps` step-progress
  read that renders a missing boot row as `pending`),
  [ADR-0141](0141-failed-run-reason-surfacing.md) (the `runs.get` envelope shape it
  extends, and the precedent of reaching a job by `dedup_key` to surface a redacted reason).

## Context

After a boot job terminally fails, `runs.get` reports `data.steps.boot:"pending"` —
indistinguishable from "boot never attempted" and ambiguous about whether a retry is in
flight (#750, black-box Part 2 review follow-up).

The cause is a deliberate design seam. The `run_steps` ledger has only `running` /
`succeeded` states (`db/idempotency.py`, CHECK in `db/schema/0043_run_steps_state_check.sql`).
On boot failure the step row is **deleted** (`jobs/handlers/runs_boot.py` →
`abandon_run_step_best_effort` → `DELETE FROM run_steps WHERE state='running'`), so a clean
in-place retry can recycle the step (ADR-0185). `step_progress` renders the now-missing row as
`pending` (`services/runs/steps.py`), which `envelope_for_run` surfaces in `data.steps`
(`mcp/tools/lifecycle/runs/common.py`). A failed boot therefore reverts to `pending`.

The evidence the agent needs already exists on the read path, just not in the ledger. The boot
job is enqueued with a deterministic `dedup_key = f"{run_id}:boot"`
(`mcp/tools/lifecycle/runs/steps.py`). On a terminal failure the worker marks that job `failed`
and records its `error_category` (`jobs/worker.py`). On a retry the **same row** is recycled in
place — reset to `queued` with `error_category` cleared (`jobs/queue.py`
`enqueue(..., retry_terminal_failed=True)`). So at any instant there is at most one boot job per
Run, reachable by `dedup_key`, whose `state` + `error_category` already encode the three-way
distinction: no row (never attempted), `queued`/`running` (attempt/retry in flight), `failed`
(terminally failed).

`runs.get` already reaches the jobs table on a neighboring path: the failed-**Run** envelope
loads the Run's `failing_job_id` job (ADR-0141). But a failed *boot* does not fail the *Run* —
the Run stays `SUCCEEDED` (build succeeded; install/boot live in `run_steps`), so
`failing_job_id` is unset and that path does not fire. The boot job must be reached by its
`dedup_key`, the same mechanism admission already uses to surface a terminal `provision` job's
reason (ADR-0149).

## Decision

On the `runs.get` read path, when the boot step is not `succeeded`, look up the boot job by its
deterministic `dedup_key` and, **only when that job is in a terminal `failed` state**, attach a
small `boot_readiness` object to the envelope `data`:

```jsonc
"boot_readiness": {
  "job_id": "<uuid>",
  "status": "failed",
  "error_category": "readiness_failure"  // or null if the job recorded none
}
```

1. **A typed read helper.** Add `failed_boot_attempt(conn, run_id) -> BootAttempt | None` to
   `services/runs/steps.py`. It calls `queue.get_by_dedup_key(conn, f"{run_id}:boot")` and
   returns a frozen `BootAttempt(job_id, error_category)` **iff** the job exists and its `state`
   is `JobState.FAILED`; otherwise `None`. A missing job, or a `queued`/`running`/`succeeded`
   job, yields `None`. `BootAttempt.as_data()` renders the fixed-key dict above.

2. **Gate it on the read path.** `get_run` (`mcp/tools/lifecycle/runs/view.py`) already
   computes `progress` for a `SUCCEEDED` Run. When `progress` exists and
   `progress.boot != "succeeded"`, it calls `failed_boot_attempt` and threads the result into
   the envelope. For a non-`SUCCEEDED` Run (no `progress`), or a Run whose boot already
   `succeeded`, the lookup is skipped — there is no failed boot to report.

3. **Render it in the envelope.** `envelope_for_run` gains a keyword-only
   `boot_readiness: BootAttempt | None = None`; on the `SUCCEEDED` branch, when non-`None`, it
   sets `data["boot_readiness"] = boot_readiness.as_data()`. Every other caller and branch keeps
   the `None` default, so `runs.list`, the failed-Run envelope, and the `CREATED`/`RUNNING`/
   `CANCELED` branches are untouched.

`status` is always `"failed"` (the field exists so a future non-failed surfacing is additive,
and so the agent reads an explicit status rather than inferring it from presence). `job_id` is
the boot job's primary key — an opaque UUID for the caller's own Run, carrying no cross-project
signal. `error_category` is the `ErrorCategory` value the worker recorded, or `null`.

## Consequences

- `runs.get` on a `SUCCEEDED` Run whose boot terminally failed now carries
  `data.boot_readiness` alongside the unchanged `data.steps.boot:"pending"`. The agent can
  distinguish failed-boot from never-booted and read the failure category without an
  out-of-band `jobs.*` lookup.
- A never-attempted boot (no boot job) and a retry in flight (`queued`/`running` boot job) both
  keep `steps.boot:"pending"` with **no** `boot_readiness` — the existing pending semantics are
  exactly right for "nothing decided yet", and emitting an `error_category` for an in-flight job
  (which has none) would be misleading.
- One extra DB round-trip on `runs.get`, **only** for a `SUCCEEDED` Run whose boot has not
  succeeded (a single indexed `SELECT … WHERE dedup_key = …` on the UNIQUE column). A booted Run
  and a non-`SUCCEEDED` Run pay nothing.
- No MCP request-shape, migration, authz, or dependency change. `runs.get` advertises the
  shared generic envelope outputSchema (`ENVELOPE_OUTPUT_SCHEMA`, #565/ADR-0170) where `data` is
  a free-form object, so the new `data.boot_readiness` key invalidates no committed
  schema or generated-doc snapshot.
- `error_category` is an enum value, `job_id` a UUID, `status` a literal — none is guest,
  console, or gdb output, so no redaction applies. (The boot job's `failure_context` *message*
  is **not** surfaced here; only the category. The Run-level failed envelope already owns
  message surfacing via ADR-0141's redaction-checked path.)
- The `run_steps` retry design (ADR-0185) is untouched: no `failed` state, no migration, no
  change to the abandon/recycle flow. This ADR reads around the deletion, it does not undo it.

## Considered & rejected

- **Add a terminal `failed` state to the `run_steps` ledger** (enum + migration + CHECK + a
  writer that records the failed row instead of deleting it). Rejected: it collides directly
  with ADR-0185's recycle-on-retry design — the row is deleted *so that* a retry can recreate it
  cleanly; a persisted `failed` row would have to be special-cased on every recycle, reopening a
  settled decision. It also needs its own migration and ADR for a problem the additive read
  fully solves. This is the issue's own "heavier alternative, noted for the record".
- **Surface `boot_readiness` whenever the boot job exists, regardless of state.** Rejected:
  a `queued`/`running` job has no `error_category` and represents "in flight, undecided", which
  `steps.boot:"pending"` already conveys; emitting an attempt object with a null/absent category
  for an in-flight retry invites the agent to treat an in-progress boot as failed.
- **Put the evidence in `refs`.** Rejected: `refs` is for object-store artifact keys
  (`kernel`/`debuginfo`/`console`). Boot-attempt status is structured metadata, not an artifact
  pointer, so it belongs in `data` next to `steps` and `required_cmdline`.
- **Surface the boot job's `failure_context` message, like the Run-level failed envelope.**
  Rejected for this issue: the message needs the ADR-0123/0141 redaction-and-suppression seam,
  which the Run-level path owns; the acceptance criterion asks only for `status` +
  `error_category`, both leak-safe. Surfacing the message can be a later additive change if an
  agent needs it.
- **Reach the boot job through a new `runs`↔`jobs` column.** Rejected: the deterministic
  `dedup_key` is already the natural key (ADR-0149 precedent), so no schema change is needed.
