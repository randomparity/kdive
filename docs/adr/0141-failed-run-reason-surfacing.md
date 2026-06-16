# ADR 0141 — Surface a failed Run's failure reason on `runs.get`

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-16
- **Deciders:** KDIVE maintainers

## Context

A failed Run hides its failure reason. `runs.get` on a build-failed Run returns
`status: failed` with `error_category` only and no human-readable message; the actual reason
lives on a *separately-tracked* BUILD job's `failure_context`, reachable only via `jobs.get`
on a job id the caller must already know out-of-band. Found during black-box MCP evaluation
(D6, issue [#486](https://github.com/randomparity/kdive/issues/486)).

The relevant facts (confirmed against code):

- The `Run` row stores only the failure **category** (`failure_category: ErrorCategory | None`,
  `src/kdive/domain/models.py`); there is no message field on the Run.
- On build failure, `_fail_build` (`src/kdive/jobs/handlers/runs.py`) flips the Run to `failed`
  and records `failure_category`, but does **not** link or copy the job's reason.
- The reason **does** exist on the job: the worker writes a `failure_context: dict[str, str]`
  (carrying a `failure_message` key) at dead-letter time, and that context is **already routed
  through the secret `Redactor`** at the worker boundary (`jobs/worker.py:_failure_context`).
  `ToolResponse.from_job` already surfaces it for `jobs.get`.
- `runs.get`'s failure envelope (`envelope_for_run` in
  `src/kdive/mcp/tools/lifecycle/runs/common.py`) emits category only, no message, no job link.

The constraint is the no-leak/redaction seam (ADR-0123/0097): author-controlled detail is safe
to surface; raw exception text must not leak for the suppressed categories
(`authorization_denied`, `not_found`). `suppressed_detail()` already enforces this at the
envelope construction boundary.

## Decision

We will **link the failing job from the Run and surface its already-redacted reason on
`runs.get`**, in the granted file scope, without duplicating redaction.

1. **`failing_job_id` on the Run.** Add a nullable `failing_job_id uuid` column to `runs`
   (migration `0038`) and a `failing_job_id: UUID | None` field to the `Run` model. It is a
   **plain column, not a foreign key** — `jobs` rows are never deleted (no retention/purge path
   exists), so there is nothing to dangle, and a FK would impose insert-ordering on Run creation
   for no integrity gain. `_fail_build` sets `failing_job_id = job.id` atomically with the
   `running -> failed` transition it already performs (it already holds the `Job`).

2. **Surface the reason on `runs.get`.** When a Run is `failed`, `get_run` fetches the linked
   job via `JOBS.get(run.failing_job_id)` and passes it to `envelope_for_run`, which adds to the
   failure envelope:
   - `detail`: the job's `failure_context["failure_message"]` (the worker-redacted reason),
     routed through `ToolResponse.failure(..., detail=...)` so the no-leak seam
     (`suppressed_detail`) governs it exactly as for every other failure envelope; and
   - `data["failing_job_id"]` plus any `failure_detail_*` keys the worker recorded, so the caller
     can `jobs.get` for the full context.

   The surfaced `failure_context` is the **same already-redacted bytes** `jobs.get` returns; no
   new client egress of un-redacted text is introduced.

3. **No new redaction logic.** Because the worker already redacts `failure_context` once, at the
   only boundary that owns the op's resolved secret set, the Run-side surface reuses it verbatim.
   `runs.get` does not re-run the redactor (it has no secret set) and does not read `str(exc)`.

## Consequences

- **Migration `0038`** is additive, forward-only (ADR-0015): a nullable `uuid` column, NULL for
  every existing Run and for any failed path that does not set it (e.g. a Run failed by the
  reconciler on a torn-down System, which has no job). The envelope degrades cleanly: no
  `failing_job_id` ⇒ no `detail`/job-link, same as today.
- **A failed Run is debuggable from `runs.get` alone** for the build path, satisfying the
  acceptance criterion. The caller still *can* `jobs.get` for the full context via the surfaced
  id, but no longer *must* know that id out-of-band.
- **No-leak seam unchanged.** `detail` flows through `suppressed_detail`; the build-failure
  categories (`build_failure`, `infrastructure_failure`, `configuration_error`, …) are
  diagnostic, not suppressed, and the message they carry is the worker-redacted `failure_message`,
  not raw exception text. A `not_found`/`authorization_denied` Run failure (none is produced on
  the build path today) would still surface the seam constant.
- **Brief link-before-context window.** `_fail_build` sets `failing_job_id` and flips the Run
  `failed` *before* the worker writes the job's `failure_context` (the worker writes it in
  `queue.fail` after the handler re-raises). A `runs.get` in that window returns the job link with
  an empty/absent `detail`; once the job is terminal the `detail` is present. The link is always
  actionable; the reason fills in within the same job-completion turn. This is acceptable because
  the agent polls the Run to terminal anyway.
- **One additive model field + one read-path fetch.** `get_run` already opens a connection and
  resolves the System/runtime; the job fetch is one extra `SELECT` on the failed branch only.

## Alternatives considered

- **Copy the job's `failure_message` into a new `runs.failure_message` column.** Rejected: the
  message is *not yet computed* when `_fail_build` runs — the worker builds and redacts it later,
  in `queue.fail`. Copying it on the Run would require either re-running the redactor inside the
  handler (duplicating the worker's redaction with a second, separately-maintained code path that
  could drift) or a second write after the job dead-letters (new ordering/coupling). Linking the
  job and reading its already-redacted context is strictly less machinery and single-sources the
  redaction.
- **Set `failing_job_id` at enqueue time (`runs.build`), not at failure.** The issue lists this
  first. Rejected for *this* change because it pulls `mcp/tools/lifecycle/runs/build.py` (and the
  install/boot enqueue in `steps.py`) into scope, which sibling issue #481 also edits; setting it
  on the failure path keeps the change inside the granted scope (`handlers/runs.py`) and means the
  column is populated *exactly* for the failed Runs that need it, not for every in-flight Run.
  A future change can broaden it to enqueue time if a non-failed Run ever needs its job link.
- **In `runs.get`, scan the `jobs` table for the Run's BUILD job by payload** (no new column).
  Rejected: a `payload->>'run_id'` scan with no index is an unbounded read on a hot table, and a
  Run can have multiple jobs (build/install/boot) — the failing one is ambiguous without the
  explicit link. The column records *which* job failed unambiguously.
- **Echo `str(exc)` directly on the Run failure envelope, redacting in `runs.get`.** Rejected:
  `runs.get` has no resolved secret set, so it cannot redact correctly; the worker is the only
  place that owns the op's secret registry. Surfacing the worker's already-redacted
  `failure_context` is the correct seam.
