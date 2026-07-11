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

| Run state | linked job state / `failure_context` | `runs.get` failure envelope |
|-----------|--------------------------------------|------------------------------|
| `failed`  | job `failed` (dead-lettered) w/ `{failure_message: "..."}` | `detail="..."`, `data.failing_job_id` set |
| `failed`  | job `queued`/`running` (mid-retry, see below) — `failure_context = '{}'` | `detail=None`, `data.failing_job_id` set (link only) |
| `failed`  | `failing_job_id` NULL (reconciler-failed, no job) | `detail=None`, no `failing_job_id` (today's shape) |
| `failed`  | `failing_job_id` set, job row gone (impossible today — no purge path) | `detail=None`, `data.failing_job_id` set |
| not failed | — | unchanged success envelope |

### Multi-attempt builds — the link precedes a stable reason

BUILD jobs retry (`max_attempts=3`). `_build_and_record` rebuilds on every attempt while
`existing_build_result` is `None`, and `_fail_build` flips the Run `running -> failed` on the
**first** failing attempt; subsequent attempts find the Run already terminal and their
`_fail_build` no-ops via the existing `IllegalTransition` warn path (pre-existing behaviour,
unchanged here). Meanwhile `queue.fail` **resets `failure_context` to `'{}'` on each non-terminal
requeue** and writes the real `failure_context` only when the job finally dead-letters (last
attempt or a `terminal` error).

Consequences this feature exposes (none a regression — it makes existing state visible):

- Setting `failing_job_id` in the first `_fail_build` is correct: it is the same `Job` across all
  attempts (one row, requeued, not re-enqueued), so the link is stable from attempt 1.
- Between attempts the linked job is `queued`/`running` with `failure_context = '{}'`, so
  `runs.get` returns the **link with `detail=None`**. An agent that polls `runs.get` to a stable
  reason should treat an empty `detail` on a `failed` Run as "reason not yet finalized; the linked
  job is still retrying" and re-poll (or `jobs.get` the link to see live job state). This is the
  intended degrade, not an error.
- The surfaced `detail`, once present, reflects the **last** attempt's failure (the only one
  `queue.fail` persists). Earlier-attempt messages are not retained — acceptable: the terminal
  reason is the actionable one, and per-attempt history is out of scope.

## Edge / error paths to test (behaviour, not implementation)

- Build-failed Run: `runs.get` returns the redacted `failure_message` as `detail` and the
  `failing_job_id` in `data`; the message equals what `jobs.get` on that id returns.
- `_fail_build` writes `failing_job_id` atomically with the terminal transition; a concurrent
  cancel that wins leaves the Run terminal *without* the build link (the existing
  `IllegalTransition` warn path), and the envelope degrades to `detail=None`.
- Failed Run with `failing_job_id = NULL` (no job — e.g. reconciler): envelope carries no
  `detail` and no `failing_job_id`, identical to today.
- A failing job whose `failure_context` is empty (mid-retry requeue, or failed before the worker
  wrote context): link present, `detail=None`.
- Multi-attempt build: `failing_job_id` is set once (first `_fail_build`) and stays pointed at the
  same job row across requeues; the no-op second `_fail_build` does not overwrite or clear it.
- No-leak: a (hypothetical) `not_found`/`authorization_denied` failed Run surfaces the seam
  constant and **no job-derived data at all** (no `failing_job_id`, no `failure_detail_*`) —
  `detail` is suppressed by `ToolResponse.failure`, and the `data` extras (which bypass that
  seam) are gated in `_failed_envelope` on the same suppressed-category check
  (`suppressed_detail(category, None) is not None`). Defence-in-depth: the build path never
  produces a suppressed category, but the seam — not the producer — enforces no-leak (ADR-0123).

## Out of scope

- Surfacing install/boot job reasons (those failures do not flip the Run via `_fail_build`
  today; the column generalizes to them later with no schema change).
- Setting `failing_job_id` at enqueue time for non-failed Runs.
