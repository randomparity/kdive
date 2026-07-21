"""`runs.set` MCP handler — the post-hoc outcome note on a Run (ADR-0415, #1386)."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

OUTCOME_NOTE_MAX_LEN = 4096
"""Maximum outcome-note length in Unicode code points (mirrors the DB CHECK in 0075)."""

_INVALID_OUTCOME_NOTE_REASON = "invalid_outcome_note"


def _reject_outcome_note() -> CategorizedError:
    """Build the uniform invalid-note error, naming the rule but never the value (ADR-0123)."""
    return CategorizedError(
        f"outcome_note must be at most {OUTCOME_NOTE_MAX_LEN} characters and contain no NUL",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": _INVALID_OUTCOME_NOTE_REASON},
    )


def validate_outcome_note(note: str) -> str | None:
    """Validate and normalize a caller-supplied outcome note.

    The note is the caller's own free-form input (multi-line is allowed), echoed back verbatim
    like ``label`` — not machine output — so it is length-validated rather than run through the
    secret redactor.

    Args:
        note: The caller-supplied outcome note. An empty or whitespace-only value clears any
            existing note.

    Returns:
        ``None`` when the stripped note is empty (a clear), else the surrounding-whitespace-
        stripped note.

    Raises:
        CategorizedError: ``configuration_error`` (``details["reason"] ==
            "invalid_outcome_note"``) when the stripped note exceeds ``OUTCOME_NOTE_MAX_LEN``
            code points or contains a NUL. The message names the rule only, never the value.
    """
    cleaned = note.strip()
    if cleaned == "":
        return None
    if len(cleaned) > OUTCOME_NOTE_MAX_LEN or "\x00" in cleaned:
        raise _reject_outcome_note()
    return cleaned


async def set_run(
    pool: AsyncConnectionPool, ctx: RequestContext, run_id: str, *, outcome_note: str
) -> ToolResponse:
    """Set or clear a Run's post-hoc ``outcome_note`` (ADR-0415, #1386).

    The note is editable at any time after create, including on a terminal Run — it records the
    agent's verdict once the outcome is known, distinct from the write-once ``label``. A blank
    ``outcome_note`` clears any existing value. Requires ``contributor`` on the Run's project.

    Args:
        pool: The connection pool.
        ctx: The authenticated request context.
        run_id: The Run to annotate.
        outcome_note: The note to record; blank clears it.

    Returns:
        A compact ``annotated`` success envelope echoing ``data.outcome_note`` (and
        ``data.run_state``) on success — never a failed-Run *read* envelope, so annotating a
        terminal Run is not shaped as an error; a failure envelope (``not_found`` /
        ``configuration_error``) otherwise. Read the full Run view with ``runs.get``.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    try:
        normalized = validate_outcome_note(outcome_note)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(run_id, exc)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            await _set_locked(conn, ctx, run, normalized)
    return _annotated_response(run, normalized)


async def _set_locked(
    conn: AsyncConnection, ctx: RequestContext, run: Run, note: str | None
) -> None:
    """Persist the note under the per-Run lock and record the audit event."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await conn.execute("UPDATE runs SET outcome_note = %s WHERE id = %s", (note, run.id))
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.set",
                object_kind="runs",
                object_id=run.id,
                transition="set",
                args={"outcome_note": "cleared" if note is None else "set"},
                project=run.project,
            ),
        )


def _annotated_response(run: Run, note: str | None) -> ToolResponse:
    """Render the note-set acknowledgement, echoing the recorded ``outcome_note``.

    A success envelope regardless of the Run's lifecycle state — a set on a terminal (failed)
    Run is still a successful mutation, so it is never shaped as an error (a failed-Run *read*
    envelope carries an error status, which a successful annotation must not). The Run's state is
    carried as `data.run_state`; `runs.get` renders the full read view.
    """
    data: dict[str, JsonValue] = {
        "project": run.project,
        "run_state": run.state.value,
        "outcome_note": note,
        "label": run.label,
    }
    return ToolResponse.success(
        str(run.id), "annotated", suggested_next_actions=["runs.get"], data=data
    )
