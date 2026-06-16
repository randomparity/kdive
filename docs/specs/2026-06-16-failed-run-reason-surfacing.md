# Failed-run reason surfacing on `runs.get` (#486)

- **Status:** Draft
- **Date:** 2026-06-16
- **ADR:** [0141](../adr/0141-failed-run-reason-surfacing.md)
- **Issue:** [#486](https://github.com/randomparity/kdive/issues/486)

## Problem

`runs.get` on a build-failed Run returns `status: failed` + `error_category` with **no
message and no link** to the failing job. The reason exists only on a separately-tracked
BUILD job's redacted `failure_context`, reachable via `jobs.get` on a job id the caller must
already know out-of-band. A failed Run is therefore not debuggable from `runs.get` alone.

## Acceptance criterion (from the issue)

`runs.get` on a failed Run gives the caller an actionable reason (or a direct link to the job
that carries it) **without out-of-band knowledge of the build job id**, while respecting the
no-leak/redaction seam (ADR-0123).

## Design

See ADR-0141 for the decision and rejected alternatives. In summary:

1. **`failing_job_id` on the Run.** New nullable, non-FK `failing_job_id uuid` column on `runs`
   (migration `0038`) + `Run.failing_job_id: UUID | None`. `_fail_build`
   (`src/kdive/jobs/handlers/runs.py`) sets it to `job.id` in the same `UPDATE` that flips the
   Run `running -> failed`.

2. **Surface on `runs.get`.** `get_run` (`src/kdive/mcp/tools/lifecycle/runs/view.py`), when the
   Run is `failed` and `failing_job_id` is set, fetches the job (`JOBS.get`) and passes it to
   `envelope_for_run` (`.../runs/common.py`). The failure envelope then carries:
   - envelope `detail` = the job's `failure_context["failure_message"]` (worker-redacted),
     routed through `ToolResponse.failure(..., detail=...)` so `suppressed_detail` governs it;
   - `data["failing_job_id"]` = the job id, so the caller can `jobs.get` for full context;
   - any `failure_detail_*` keys the worker recorded, copied verbatim from the redacted context.

3. **No new redaction.** `failure_context` is already redacted once at the worker boundary
   (`jobs/worker.py:_failure_context`). `runs.get` surfaces those same bytes; it has no secret
   set and runs no redactor and never reads `str(exc)`.

## Behavioural contract

| Run state | `failing_job_id` | job `failure_context` | `runs.get` failure envelope |
|-----------|------------------|------------------------|------------------------------|
| `failed`  | set, job terminal w/ message | `{failure_message: "..."}` | `detail="..."`, `data.failing_job_id` set |
| `failed`  | set, job not yet dead-lettered | empty | `detail=None`, `data.failing_job_id` set (link only) |
| `failed`  | NULL (reconciler-failed) | n/a | `detail=None`, no `failing_job_id` (today's shape) |
| `failed`  | set, job row gone (impossible today) | n/a | `detail=None`, `data.failing_job_id` set |
| not failed | — | — | unchanged success envelope |

## Edge / error paths to test (behaviour, not implementation)

- Build-failed Run: `runs.get` returns the redacted `failure_message` as `detail` and the
  `failing_job_id` in `data`; the message equals what `jobs.get` on that id returns.
- `_fail_build` writes `failing_job_id` atomically with the terminal transition; a concurrent
  cancel that wins leaves the Run terminal *without* the build link (the existing
  `IllegalTransition` warn path), and the envelope degrades to `detail=None`.
- Failed Run with `failing_job_id = NULL` (no job — e.g. reconciler): envelope carries no
  `detail` and no `failing_job_id`, identical to today.
- A failing job whose `failure_context` is empty (failed before the worker wrote context):
  link present, `detail=None`.
- No-leak: a (hypothetical) `not_found`/`authorization_denied` failed Run surfaces the seam
  constant, never the job message — proven by routing through `suppressed_detail`.

## Out of scope

- Surfacing install/boot job reasons (those failures do not flip the Run via `_fail_build`
  today; the column generalizes to them later with no schema change).
- Setting `failing_job_id` at enqueue time for non-failed Runs.
