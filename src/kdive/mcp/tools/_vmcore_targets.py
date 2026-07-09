"""Shared Run → build → vmcore target resolution for MCP read tools."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.artifact_queries import raw_vmcore_key
from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import existing_build_result

# Structured `reason` tokens for the distinct vmcore-target preconditions (#487, ADR-0142).
# The token rides in the `not_found` error's `details` (unsuppressed `data`, ADR-0123) and keys
# `_VMCORE_NEXT_ACTIONS`. Author-controlled; never derived from guest/exception/resource text.
NO_DEBUGINFO = "no_debuginfo"
NO_BUILD = "no_build"
NO_VMCORE = "no_vmcore"

# The early-boot console-crash kind, declared on the Run as ``expected_boot_failure`` (ADR-0064).
# When a Run with this kind resolves to no vmcore, the kdump capture kernel never loaded (the
# crash precedes kexec), so no core is expected by design and the console artifact is the evidence
# source. The kind is carried on the ``NO_VMCORE`` error so the postmortem handler can redirect to
# the console (#734, ADR-0227).
CONSOLE_CRASH = "console_crash"

# `data.reason` for the postmortem console-crash redirect envelope (#734, ADR-0227). Distinct from
# `no_vmcore` so a client can branch on "no core is expected by design" vs "no core captured yet".
EXPECTED_CONSOLE_CRASH = "expected_console_crash"

# reason token -> literal next tool names. An absent or unknown reason (e.g. the absent-Run /
# ungranted-project miss, which carries no reason so the envelope cannot leak membership) maps to
# no next actions.
_VMCORE_NEXT_ACTIONS: dict[str, list[str]] = {
    NO_DEBUGINFO: ["runs.get", "runs.complete_build"],
    NO_BUILD: ["runs.complete_build", "runs.get"],
    NO_VMCORE: ["vmcore.fetch", "runs.get"],
}


class RunVmcoreTarget(NamedTuple):
    """The resolved inputs needed to analyze a Run's captured vmcore."""

    debuginfo_ref: str
    build_id: str
    vmcore_ref: str


async def resolve_run_vmcore_target(
    conn: AsyncConnection, ctx: RequestContext, run_id: str
) -> RunVmcoreTarget:
    """Resolve debuginfo ref, build-id, and raw vmcore key for a viewer-authorized Run.

    A malformed ``run_id`` is a parse failure (``configuration_error``). A syntactically valid
    id that resolves to no visible Run — absent, in an ungranted project (no-leak), or missing a
    prerequisite target artifact — is ``not_found`` (ADR-0097). The preconditions are checked
    most-operative-first for these vmcore-centric callers (ADR-0165): no captured core
    (``no_vmcore``), then null ``debuginfo_ref`` (``no_debuginfo``), then no recorded build
    (``no_build``). A never-booted Run therefore reports ``no_vmcore`` (its operative gap), while
    a Run with a captured core but missing symbolization/provenance inputs still reports the
    precise ``no_debuginfo`` / ``no_build`` reason. The two helpers stay distinct so the malformed
    branch cannot drift into ``not_found``.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        raise _target_config_error()
    run = await RUNS.get(conn, uid)
    if run is None or run.project not in ctx.projects:
        raise _target_not_found()
    require_role(ctx, run.project, Role.VIEWER)
    vmcore_ref = await raw_vmcore_key(conn, uid)
    if vmcore_ref is None:
        raise _no_vmcore_not_found(run)
    if run.debuginfo_ref is None:
        raise _precondition_not_found(NO_DEBUGINFO)
    build_id = await _build_id_for_run(conn, uid)
    if build_id is None:
        raise _precondition_not_found(NO_BUILD)
    return RunVmcoreTarget(run.debuginfo_ref, build_id, vmcore_ref)


def vmcore_target_failure(run_id: str, exc: CategorizedError) -> ToolResponse:
    """Map a :func:`resolve_run_vmcore_target` miss to its failure envelope (#487, ADR-0142).

    Attaches the reason-keyed ``suggested_next_actions`` so a caller learns which precondition is
    unmet and the next tool to call. The ``reason`` token rides in ``data`` (unsuppressed); the
    ``not_found`` ``detail`` stays the suppressed constant (no-leak seam, ADR-0123). An absent or
    unknown reason (the absent-Run / ungranted-project miss) yields no next actions.
    """
    reason = exc.details.get("reason")
    actions = _VMCORE_NEXT_ACTIONS.get(reason) if isinstance(reason, str) else None
    return ToolResponse.failure_from_error(run_id, exc, suggested_next_actions=actions)


def _target_config_error() -> CategorizedError:
    return CategorizedError(
        "run_id is not a uuid",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _target_not_found() -> CategorizedError:
    return CategorizedError(
        "run does not resolve to a captured vmcore target",
        category=ErrorCategory.NOT_FOUND,
    )


def _precondition_not_found(reason: str) -> CategorizedError:
    return CategorizedError(
        "run does not resolve to a captured vmcore target",
        category=ErrorCategory.NOT_FOUND,
        details={"reason": reason},
    )


def _no_vmcore_not_found(run: Run) -> CategorizedError:
    """The ``NO_VMCORE`` miss, carrying the console-crash kind iff the Run declared it (#734).

    The kind is attached to ``details`` **only** when ``run.expected_boot_failure.kind`` is
    exactly ``console_crash``: the non-console-crash miss falls through ``vmcore_target_failure``
    → ``safe_error_details`` (which forwards every scalar to ``data``), so an unconditional kind
    would surface a new ``data.expected_boot_failure`` key on the unchanged envelope the day a
    second kind exists (ADR-0227). Conditional attachment keeps that envelope's ``data`` exactly
    ``{reason: no_vmcore}``.
    """
    details: dict[str, object] = {"reason": NO_VMCORE}
    boot_failure = run.expected_boot_failure
    if isinstance(boot_failure, dict) and boot_failure.get("kind") == CONSOLE_CRASH:
        details["expected_boot_failure"] = CONSOLE_CRASH
    return CategorizedError(
        "run does not resolve to a captured vmcore target",
        category=ErrorCategory.NOT_FOUND,
        details=details,
    )


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    return None if result is None else result.build_id
