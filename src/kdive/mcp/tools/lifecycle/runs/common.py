"""Shared helpers for lifecycle run MCP tool lanes."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from kdive.domain.capacity.state import RunState
from kdive.domain.errors import ErrorCategory, suppressed_detail
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import Job
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import job_envelope
from kdive.services.runs import states as run_states
from kdive.services.runs.steps import StepProgress

ALLOC_HOSTABLE = run_states.ALLOC_HOSTABLE
INVESTIGATION_OPEN_FOR_RUN = run_states.INVESTIGATION_OPEN_FOR_RUN
RUN_BUILD_TERMINAL = run_states.RUN_BUILD_TERMINAL
RUN_HOSTABLE = run_states.RUN_HOSTABLE
RUN_NON_TERMINAL = run_states.RUN_NON_TERMINAL
SYSTEM_GONE = run_states.SYSTEM_GONE

# A Run holds its System until terminal; at most one non-terminal Run per System.

# A failed Run with no linked job (e.g. a reconciler-driven failure on a torn-down System,
# ADR-0141) would otherwise surface only its `failure_category` with no actionable reason. Each
# diagnostic category the no-job path can carry maps to a fixed, resource-free reason so the
# failed Run is never bare (#516). Strings name the failing condition, not any project/host/id,
# so they carry no resource-existence signal. No-leak categories (ADR-0123) are absent here —
# they stay routed through the seam constant; `_NO_JOB_FALLBACK` covers any unmapped diagnostic
# category so the surface never regresses to category-only.
_NO_JOB_FALLBACK = "Run failed before a job recorded a reason"
_NO_JOB_DETAIL: dict[ErrorCategory, str] = {
    ErrorCategory.LEASE_EXPIRED: (
        "Run failed: its lease expired and the reconciler reclaimed the System"
    ),
    ErrorCategory.INFRASTRUCTURE_FAILURE: (
        "Run failed for an infrastructure reason with no job to record details"
    ),
    ErrorCategory.PROVISIONING_FAILURE: "Run failed while provisioning the System",
    ErrorCategory.ALLOCATION_DENIED: "Run failed: the System's allocation was reclaimed",
    ErrorCategory.QUEUE_TIMEOUT: "Run failed: it timed out waiting in the queue",
}


def no_job_failure_detail(category: ErrorCategory) -> str:
    """Return the fixed, resource-free reason for a failed Run with no linked job (#516).

    Args:
        category: The Run's `failure_category` (the default-applied one, never `None`).

    Returns:
        A specific reason for a mapped diagnostic category, else a generic fallback, so a
        failed Run is never surfaced as a bare category.
    """
    return _NO_JOB_DETAIL.get(category, _NO_JOB_FALLBACK)


def _succeeded_next_step(run: Run, progress: StepProgress | None) -> list[str]:
    """Second action(s) for a `SUCCEEDED` Run, walking the real progression (ADR-0179).

    Keys the booted-run branch on the observed `boot_outcome` (from the boot step result),
    not the Run's create-time `expected_boot_failure`: a Run that expected a crash but booted
    normally is live-debuggable. The `postmortem.triage` / `vmcore.fetch` pair matches the
    failure `sessions_lifecycle.py` returns for a live attach on an `expected_crash_observed`
    boot.
    """
    if run.system_id is None:
        return ["runs.bind"]
    if progress is None or progress.install != "succeeded":
        return ["runs.install"]
    if progress.boot != "succeeded":
        return ["runs.boot"]
    if progress.boot_outcome == "expected_crash_observed":
        return ["postmortem.triage", "vmcore.fetch"]
    return ["debug.start_session"]


def envelope_for_run(
    run: Run,
    *,
    required_cmdline: str | None = None,
    failing_job: Job | None = None,
    active_debug_session_ids: list[str] | None = None,
    step_progress: StepProgress | None = None,
) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`.

    When the Run is `failed` and `failing_job` (the Run's `failing_job_id` job) is supplied,
    the envelope also surfaces the job's already-worker-redacted `failure_message` as `detail`
    and the `failing_job_id` in `data`, so the caller gets an actionable reason without
    out-of-band knowledge of the job id (ADR-0141). The `detail` is routed through
    `ToolResponse.failure`, so the no-leak seam (`suppressed_detail`, ADR-0123) governs it; no
    new redaction runs here — the worker already redacted `failure_context`.

    `active_debug_session_ids` (ADR-0176) lists the ids of `attach`/`live` debug sessions on
    this Run so a recovering agent can pivot from a known Run to a live session handle. The
    Run is already project-scoped before this is built, so the ids carry no cross-project
    signal. Surfaced only on a non-failed Run (a failed Run holds no live session).
    """
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return _failed_envelope(run, category, failing_job)
    steps: dict[str, str] | None = None
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    elif run.state is RunState.SUCCEEDED:
        # Build-succeeded; install/boot live in run_steps (ADR-0179). Walk the progression
        # and surface the per-step map so a caller need not infer it from Run.state.
        actions = ["runs.get", *_succeeded_next_step(run, step_progress)]
        if step_progress is not None:
            steps = step_progress.steps_map()
    else:  # CANCELED — terminal, nothing to advance.
        actions = ["runs.get"]
    data: dict[str, JsonValue] = {
        "project": run.project,
        "target_kind": run.target_kind.value,
        "system_id": str(run.system_id) if run.system_id is not None else None,
        "active_debug_session_ids": list(active_debug_session_ids or []),
    }
    if steps is not None:
        data["steps"] = cast(JsonValue, steps)
    if required_cmdline is not None:
        data["required_cmdline"] = required_cmdline
    if run.expected_boot_failure is not None:
        kind = run.expected_boot_failure.get("kind")
        if isinstance(kind, str):
            data["expected_boot_failure"] = kind
        data["expected_boot_failure_detail"] = cast(JsonValue, run.expected_boot_failure)
    return ToolResponse.success(
        str(run.id), run.state.value, suggested_next_actions=actions, data=data
    )


def _failed_envelope(run: Run, category: ErrorCategory, failing_job: Job | None) -> ToolResponse:
    """Build the `failed` Run envelope, surfacing the linked job's redacted reason (ADR-0141).

    The job-derived surface (`detail`, `failing_job_id`, and any `failure_detail_*` keys) is
    suppressed entirely for a no-leak category (ADR-0123): `ToolResponse.failure` already
    suppresses `detail`, but the `data` extras bypass that seam, so they are gated here on the
    same rule. `suppressed_detail(category, None) is not None` is true exactly for a suppressed
    category (it returns the fixed constant even when `raw` is `None`).

    When there is no linked job (a reconciler-driven failure on a torn-down System, ADR-0141),
    `detail` is derived from the category so the failed Run is never category-only (#516). That
    derived `detail` is still routed through `ToolResponse.failure`, so a no-leak category
    surfaces the seam constant, never the derived reason.
    """
    data: dict[str, JsonValue] = {"current_status": run.state.value}
    detail: str | None = None
    no_leak = suppressed_detail(category, None) is not None
    if failing_job is not None and not no_leak:
        data["failing_job_id"] = str(failing_job.id)
        context = failing_job.failure_context
        detail = context.get("failure_message") or None
        for key, value in context.items():
            if key.startswith("failure_detail_"):
                data[key] = value
    elif failing_job is None:
        detail = no_job_failure_detail(category)
    return ToolResponse.failure(str(run.id), category, detail=detail, data=data)


def run_job_envelope(job: Job, run_id: UUID) -> ToolResponse:
    return job_envelope(job, "run_id", run_id)
