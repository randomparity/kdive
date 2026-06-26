"""Shared helpers for lifecycle run MCP tool lanes."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory, suppressed_detail
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import Job
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle._recovery import build_profile_summary
from kdive.mcp.tools.lifecycle.vmcore import CONSOLE_CRASH_GUIDANCE
from kdive.services.runs import states as run_states
from kdive.services.runs.steps import (
    READY_BOOT_OUTCOME,
    BootAttempt,
    StepProgress,
    ready_boot_outcome,
)

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


def _run_recovery(run: Run) -> dict[str, JsonValue]:
    """Investigation link + redaction-safe build summary, on the Run row (#568)."""
    return {
        "investigation_id": str(run.investigation_id),
        **build_profile_summary(run.build_profile),
    }


# The failing job's failure-context key carrying the build-log artifact id (ADR-0238); the worker
# writes it there from the build error's details, and the failed-Run envelope promotes it to
# ``refs["build-log"]``.
_BUILD_LOG_FAILURE_DETAIL = "failure_detail_build_log_artifact"


def _run_artifact_refs(
    run: Run, *, console_ref: str | None = None, build_log_ref: str | None = None
) -> dict[str, str]:
    """The Run's object-store artifact keys, for the envelope ``refs`` slot.

    ``console_ref`` is the boot step's console evidence artifact id (ADR-0226), surfaced as
    ``console`` so an agent resolves it directly via ``artifacts.get``; it is supplied only on
    the ``runs.get`` success path (which loads the boot step), and omitted when no boot step
    recorded evidence. ``build_log_ref`` is the failed build's build-log artifact id (ADR-0238),
    surfaced as ``build-log`` on the failed-Run path; omitted when the build captured no log.
    """
    refs: dict[str, str] = {}
    if run.kernel_ref:
        refs["kernel"] = run.kernel_ref
    if run.debuginfo_ref:
        refs["debuginfo"] = run.debuginfo_ref
    if console_ref is not None:
        refs["console"] = console_ref
    if build_log_ref is not None:
        refs["build-log"] = build_log_ref
    return refs


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
    boot_readiness: BootAttempt | None = None,
    build_provenance: dict[str, str] | None = None,
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

    `boot_readiness` (#750, ADR-0230) is the Run's terminally-failed boot job, surfaced as
    `data.boot_readiness` on the `SUCCEEDED` success path so a caller can distinguish a failed
    boot (whose `run_steps` row was deleted to `pending` by the ADR-0185 recycle) from a
    never-attempted one. The read path passes it only when the boot step is not yet succeeded.
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
    if boot_readiness is not None:
        # The boot step row was deleted on terminal failure (ADR-0185), so `steps.boot` reads
        # `pending`; surface the surviving failed boot job as evidence (#750, ADR-0230). Only the
        # SUCCEEDED read path passes a non-None value, so this stays scoped to `runs.get`.
        data["boot_readiness"] = cast(JsonValue, boot_readiness.as_data())
    if (
        step_progress is not None
        and step_progress.boot_outcome == READY_BOOT_OUTCOME
        and run.target_kind is ResourceKind.LOCAL_LIBVIRT
    ):
        # The success-path symmetry to the failure side: name what defined boot success (the
        # kdive-ready console marker reached with no pre-marker crash) so the agent need not scrape
        # the console to trust the verdict (#837, ADR-0254). The descriptor is single-sourced in
        # `services.runs.steps` so the wording cannot drift; it carries only build-time constants,
        # so it is redaction-safe. Gated on local-libvirt: remote-libvirt also records
        # `boot_outcome: "ready"` but confirms readiness by a boot-id change (ADR-0082), not the
        # console marker, so this console-marker descriptor would misreport a remote boot.
        data["boot_outcome"] = cast(JsonValue, ready_boot_outcome())
    if required_cmdline is not None:
        # The platform-owned boot args (#748). Extra kernel debug args are not set here: they
        # are appended via runs.build.cmdline (or runs.complete_build.cmdline for external
        # builds), bound on the Run's first build.
        data["required_cmdline"] = required_cmdline
    if run.expected_boot_failure is not None:
        kind = run.expected_boot_failure.get("kind")
        if isinstance(kind, str):
            data["expected_boot_failure"] = kind
        data["expected_boot_failure_detail"] = cast(JsonValue, run.expected_boot_failure)
    if step_progress is not None and step_progress.available_capture is not None:
        # The crash outcome's reachable-now capture methods, and the provisioned-but-inert ones,
        # so an agent learns which capture flags will not fire on this boot (#760, ADR-0239).
        data["available_capture"] = cast(JsonValue, step_progress.available_capture)
    if step_progress is not None and step_progress.inert_capture is not None:
        data["inert_capture"] = cast(JsonValue, step_progress.inert_capture)
        if step_progress.boot_outcome == "expected_crash_observed":
            # The reason the inert methods cannot fire on a console_crash boot: the panic
            # precedes kexec, so live attach/vmcore are impossible by design (#802). Reuse the
            # one shared constant that debug.start_session/vmcore.fetch surface as their failure
            # detail so the wordings cannot drift; it interpolates no guest output, so surfacing
            # it on the success path is redaction-safe. Gated on the outcome so the
            # console_crash-specific wording stays accurate.
            data["inert_capture_reason"] = CONSOLE_CRASH_GUIDANCE
    if build_provenance is not None:
        # The build-step provenance recorded at write time (Task 5, #778): remote, ref,
        # resolved_commit, build_host. Passed through verbatim — userinfo-stripped at write time.
        # Key is absent entirely when no provenance was recorded (not present-as-null).
        data["build_provenance"] = cast(JsonValue, build_provenance)
    data.update(_run_recovery(run))
    console_ref = step_progress.console_evidence_artifact_id if step_progress is not None else None
    return ToolResponse.success(
        str(run.id),
        run.state.value,
        suggested_next_actions=actions,
        refs=_run_artifact_refs(run, console_ref=console_ref),
        data=data,
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
    data: dict[str, JsonValue] = {"current_status": run.state.value, **_run_recovery(run)}
    detail: str | None = None
    build_log_ref: str | None = None
    no_leak = suppressed_detail(category, None) is not None
    if failing_job is not None and not no_leak:
        data["failing_job_id"] = str(failing_job.id)
        context = failing_job.failure_context
        detail = context.get("failure_message") or None
        build_log_ref = context.get(_BUILD_LOG_FAILURE_DETAIL) or None
        for key, value in context.items():
            if key.startswith("failure_detail_"):
                data[key] = value
    elif failing_job is None:
        detail = no_job_failure_detail(category)
    return ToolResponse.failure(
        str(run.id),
        category,
        detail=detail,
        refs=_run_artifact_refs(run, build_log_ref=build_log_ref),
        data=data,
    )


def run_job_envelope(job: Job, run_id: UUID) -> ToolResponse:
    return job_envelope(job, "run_id", run_id)
