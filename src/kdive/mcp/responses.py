"""The uniform tool-response envelope every MCP tool returns (ADR-0019).

Every tool — across all planes — returns a :class:`ToolResponse` carrying the
object id, a status, literal next tool names, artifact references, and (only for a
failure) an error category. The shape is fixed surface-wide so an agent learns one
envelope and one polling pattern, and so "references, never log dumps" is structural
rather than per-plane discipline.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import (
    CategorizedError,
    ErrorCategory,
    retryable_category,
    suppressed_detail,
)
from kdive.domain.operations.jobs import Job, JobKind
from kdive.serialization import JsonValue, safe_error_details, validate_json_value

# Literal next tool names by the job's state.
# See the design doc's suggested_next_actions table.
# A non-terminal job points back at `jobs.wait` because the row has not settled and calling
# again learns something new. Every terminal state is empty (ADR-0468): the caller was just
# handed the final envelope, so steering it at a tool that re-reads the same row is a loop.
# What is genuinely actionable after a terminal job is kind-specific and comes from
# _TERMINAL_KIND_ACTIONS below.
_NEXT_ACTIONS: dict[JobState, list[str]] = {
    JobState.QUEUED: ["jobs.wait", "jobs.cancel"],
    JobState.RUNNING: ["jobs.wait", "jobs.cancel"],
    JobState.SUCCEEDED: [],
    JobState.FAILED: [],
    JobState.CANCELED: [],
}

# Tool-specific next actions appended when a job of this kind reaches SUCCEEDED, so the hint
# is a durable property of the completed job wherever it is rendered — every
# jobs.wait / jobs.list read of the terminal job carries it, not just the enqueuing tool's
# synchronous envelope (ADR-0414). A completed TEARDOWN drives the System to torn_down but
# leaves its Allocation `active` until allocations.release; point the agent at that second
# step so the two-step wind-down is not forgotten (#1385). A completed CAPTURE_VMCORE carries the
# redacted core's artifact id in `refs.result` (ADR-0466), so it points at the two tools that
# consume that reference — read the bytes with artifacts.get, or triage the core with
# postmortem.crash. Keyed on SUCCEEDED only: a failed or canceled teardown did not free anything
# to release, and a failed capture produced no core.
_TERMINAL_KIND_ACTIONS: dict[JobKind, list[str]] = {
    JobKind.CAPTURE_VMCORE: ["artifacts.get", "postmortem.crash"],
    JobKind.TEARDOWN: ["allocations.release"],
}

# A response reports a failure under the job's `failed` lifecycle status or the
# tool-level `error` status; both require an error category, all others forbid one.
_FAILURE_STATUSES = frozenset({JobState.FAILED.value, "error"})

#: The only next action an ``authorization_denied`` envelope ever carries (ADR-0471).
#:
#: A denial used to point back at the tool that just denied the caller, steering a
#: contract-following agent into an action it cannot complete without a grant it does not hold
#: (#1596). The recovery for a denial is never the denied tool: it is a grant the server cannot
#: issue. ``session.whoami`` is the one step the caller can always take — it is in
#: ``PUBLIC_TOOLS`` (guarded in tests/mcp/core/test_responses.py), so it never denies, it names
#: the grants the caller actually holds, and it is not the denied tool, so it cannot loop.
#: Combined with ADR-0123's suppressed ``detail``, it is the caller's only route to a blocker it
#: can report to its operator.
DENIAL_NEXT_ACTIONS: tuple[str, ...] = ("session.whoami",)

ResponseData = dict[str, JsonValue]
ResponseDataInput = Mapping[str, JsonValue]


def current_status_data(current_status: str) -> dict[str, JsonValue]:
    return {"current_status": current_status}


def reason_data(reason: str) -> dict[str, JsonValue]:
    return {"reason": reason}


class ToolResponse(BaseModel):
    """The structured JSON every MCP tool returns (ADR-0019)."""

    # Reject unknown keys on validation: a dump has exactly these fields, so an envelope
    # round-tripped through model_validate (e.g. the compact-response middleware, ADR-0314)
    # must not silently drop a stray key — a superset dict raises rather than being coerced.
    model_config = ConfigDict(extra="forbid")

    object_id: str
    status: str
    suggested_next_actions: list[str] = Field(default_factory=list)
    refs: dict[str, str] = Field(default_factory=dict)
    error_category: str | None = None
    retryable: bool | None = None
    # Human-readable failure reason (ADR-0123). Present-but-null on success and on worker-plane
    # (`from_job`) envelopes; on a failure it is the seam-suppressed `CategorizedError` message.
    detail: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    items: list[ToolResponse] = Field(default_factory=list)

    @field_validator("data", mode="before")
    @classmethod
    def _data_is_json_compatible(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("data must be a JSON object")
        for key, value in data.items():
            validate_json_value(value, path=f"data.{key}")
        return data

    @model_validator(mode="after")
    def _category_iff_failed(self) -> ToolResponse:
        """Enforce category-iff-failure and derive ``retryable`` from the category.

        A failure status without a category, or any other status carrying one, is a
        producer bug — fail fast at construction (ADR-0019). ``retryable`` is a pure
        function of the category (ADR-0118), derived here so it can never drift and is
        never caller-set; ``None`` on success, a ``bool`` on a classified failure. The
        table it reads is :data:`~kdive.domain.errors.RETRYABLE_BY_CATEGORY`, shared with
        the job queue's dead-letter decision so both retry seams agree (ADR-0483).
        """
        is_failure = self.status in _FAILURE_STATUSES
        if is_failure and self.error_category is None:
            raise ValueError(f"status {self.status!r} requires an error_category")
        if not is_failure and self.error_category is not None:
            raise ValueError(f"error_category set on non-failure status {self.status!r}")
        if is_failure and self.error_category is not None:
            self.retryable = retryable_category(ErrorCategory(self.error_category))
        else:
            self.retryable = None
        return self

    @classmethod
    def success(
        cls,
        object_id: str,
        status: str,
        *,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseDataInput | None = None,
    ) -> ToolResponse:
        """Build a non-failure envelope.

        ``status`` must not be a failure status (``failed``/``error``); passing one is a
        producer bug and the model validator raises, surfacing the misuse at construction.
        """
        return cls(
            object_id=object_id,
            status=status,
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=dict(data or {}),
        )

    @classmethod
    def collection(
        cls,
        object_id: str,
        status: str,
        items: list[ToolResponse],
        *,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseDataInput | None = None,
    ) -> ToolResponse:
        """Build one envelope for a collection-returning tool."""
        payload = dict(data or {})
        payload["count"] = len(items)
        return cls(
            object_id=object_id,
            status=status,
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=payload,
            items=list(items),
        )

    @classmethod
    def failure(
        cls,
        object_id: str,
        category: ErrorCategory,
        *,
        detail: str | None = None,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseDataInput | None = None,
    ) -> ToolResponse:
        return cls(
            object_id=object_id,
            status="error",
            error_category=category.value,
            detail=suppressed_detail(category, detail),
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=dict(data or {}),
        )

    @classmethod
    def denied(cls, object_id: str, *, data: ResponseDataInput | None = None) -> ToolResponse:
        """Build the ``authorization_denied`` envelope (ADR-0471).

        The single constructor for a denial across the whole tool surface. It takes no
        ``suggested_next_actions``: the breadcrumb is fixed to :data:`DENIAL_NEXT_ACTIONS`, so a
        denial naming the tool that produced it — the #1596 defect — is unrepresentable rather
        than merely discouraged. ``tests/guards/test_denial_envelope_actions.py`` pins this as
        the only way an ``AUTHORIZATION_DENIED`` envelope is built, so a new tool cannot
        reintroduce a self-suggestion by hand-rolling ``ToolResponse.failure``.

        Args:
            object_id: The envelope's object id — the same id the served call would carry.
            data: Structured fields safe to surface under the no-leak seam (ADR-0123 suppresses
                ``detail``, not ``data``), e.g. the destructive-op gate's closed enum of failed
                policy checks (ADR-0129).
        """
        return cls.failure(
            object_id,
            ErrorCategory.AUTHORIZATION_DENIED,
            suggested_next_actions=list(DENIAL_NEXT_ACTIONS),
            data=data,
        )

    @classmethod
    def failure_from_error(
        cls,
        object_id: str,
        exc: CategorizedError,
        *,
        category: ErrorCategory | None = None,
        suggested_next_actions: list[str] | None = None,
        data: ResponseDataInput | None = None,
    ) -> ToolResponse:
        payload = safe_error_details(exc.details)
        payload.update(data or {})
        return cls.failure(
            object_id,
            category or exc.category,
            detail=str(exc),
            suggested_next_actions=suggested_next_actions,
            data=payload,
        )

    @classmethod
    def from_job(cls, job: Job, *, extra_next_actions: list[str] | None = None) -> ToolResponse:
        """Build a worker-plane job-handle envelope.

        The job id is carried in the envelope's standard ``object_id`` field (``str(job.id)``),
        not a separate ``job_id`` key — the ``{job_id, status: queued}`` shorthand in the tool
        reference docs names that same value. Pass this ``object_id`` back as the ``job_id``
        argument to ``jobs.wait`` / ``jobs.cancel``.

        ``extra_next_actions`` are tool-specific next actions appended *after* the job
        state's generic lifecycle set (order preserved, not deduplicated). The default
        ``None`` yields the lifecycle-only set every existing caller relies on; a caller
        that supplies actions is responsible for RBAC-filtering them first (ADR-0377).

        A SUCCEEDED job additionally carries its kind's ``_TERMINAL_KIND_ACTIONS`` steer
        (e.g. a completed teardown points at ``allocations.release``; ADR-0414). These are
        not RBAC-filtered here — ``from_job`` has no request context, matching the existing
        lifecycle set (``jobs.cancel`` is likewise advertised unfiltered) — because the
        worker-plane envelope's next actions are advisory and the execution-time gate on
        each named tool remains the boundary.
        """
        refs = {"result": job.result_ref} if job.result_ref else {}
        data: dict[str, JsonValue] = {"kind": job.kind.value}
        if job.state is JobState.FAILED:
            data.update(job.failure_context)
        terminal_kind = (
            _TERMINAL_KIND_ACTIONS.get(job.kind, []) if job.state is JobState.SUCCEEDED else []
        )
        actions = list(_NEXT_ACTIONS[job.state]) + terminal_kind + list(extra_next_actions or [])
        return cls(
            object_id=str(job.id),
            status=job.state.value,
            suggested_next_actions=actions,
            refs=refs,
            error_category=job.error_category.value if job.error_category else None,
            data=data,
        )
