"""`runs.complete_build` MCP handler."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_provenance import external_source_provenance
from kdive.kernel_config.gate import missing_effective_config_nudge, rootfs_mount_warning
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.catalog.artifacts.expected_uploads import EXPECTED_UPLOADS_TOOL
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    FEATURE_CONFIG_REQUIREMENTS_TOOL,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    upload_expiry_contract,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.services.runs.complete_build import (
    CompleteBuildConfigurationError,
    CompleteBuildExpiredWindowError,
    CompleteBuildFinalizer,
    CompleteBuildValidation,
    CompleteBuildValidationError,
    ExternalBuildStore,
    ObjectStoreFactory,
)
from kdive.services.runs.steps import BuildStepResult, platform_owned_cmdline_token
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import object_store_from_env


@dataclass(frozen=True, slots=True)
class CompleteBuildHandlers:
    """External-build completion handler."""

    validate_complete_build: CompleteBuildValidation | None = None
    object_store_factory: ObjectStoreFactory = object_store_from_env

    async def complete_build(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        build_id: str | None,
        cmdline: str | None = None,
        source_label: str | None = None,
        source_ref: str | None = None,
    ) -> ToolResponse:
        """Authorize and map external-build finalization to the MCP response envelope."""
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        cmdline = _normalize_cmdline(cmdline)
        owned = platform_owned_cmdline_token(cmdline)
        if owned is not None:
            return _config_error(
                run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
            )
        try:
            source_provenance = external_source_provenance(source_label, source_ref)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                return await self._complete_authorized_build(
                    conn,
                    ctx,
                    uid,
                    run_id,
                    build_id=build_id,
                    cmdline=cmdline,
                    source_provenance=source_provenance,
                )

    async def _complete_authorized_build(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        uid: UUID,
        run_id: str,
        *,
        build_id: str | None,
        cmdline: str | None,
        source_provenance: dict[str, str | bool | list[str]] | None,
    ) -> ToolResponse:
        run = await RUNS.get(conn, uid)
        if run is None or run.project not in ctx.projects:
            return _config_error(run_id)
        require_role(ctx, run.project, Role.CONTRIBUTOR)

        recorded = await _existing_build_result(conn, uid)
        if recorded is not None:
            return await self._success_envelope(conn, uid, recorded)

        service = CompleteBuildFinalizer(
            validate_complete_build=self.validate_complete_build,
            object_store_factory=self.object_store_factory,
        )
        try:
            result = await service.complete(
                conn,
                ctx,
                run,
                build_id=build_id,
                cmdline=cmdline,
                source_provenance=source_provenance,
            )
        except CompleteBuildExpiredWindowError as exc:
            return _expired_window_error(run_id, exc)
        except CompleteBuildConfigurationError as exc:
            recovery = _WINDOW_GONE_DETAIL.get(str(exc.data.get("reason")))
            if recovery is not None:
                return ToolResponse.failure(
                    run_id,
                    ErrorCategory.CONFIGURATION_ERROR,
                    detail=recovery,
                    suggested_next_actions=[CREATE_RUN_UPLOAD_TOOL],
                    data=exc.data,
                )
            return _config_error(run_id, data=exc.data)
        except CompleteBuildValidationError as exc:
            return ToolResponse.failure_from_error(
                run_id,
                exc.error,
                suggested_next_actions=[EXPECTED_UPLOADS_TOOL, CREATE_RUN_UPLOAD_TOOL],
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        return await self._success_envelope(conn, uid, result)

    async def _success_envelope(
        self, conn: AsyncConnection, uid: UUID, result: BuildStepResult
    ) -> ToolResponse:
        """Build the success envelope, attaching the boot-config warning or upload nudge.

        The two advisories are mutually exclusive: the warning keys on a *present* config missing
        boot symbols, the nudge on a config *absent* entirely (so the warning could never fire).
        Compute the nudge only when the warning is silent to avoid a second config read.
        """
        warning = await rootfs_mount_warning(conn, uid)
        nudge = None if warning is not None else await missing_effective_config_nudge(conn, uid)
        return _complete_envelope(uid, result, warning=warning, nudge=nudge)


def _expired_window_error(run_id: str, exc: CompleteBuildExpiredWindowError) -> ToolResponse:
    """Reject a lapsed upload window with the contract the mint advertised (ADR-0448).

    Self-correcting by construction: `upload_expiry_contract` is the same renderer
    `artifacts.create_run_upload` announced the deadline with, so the wall the agent was told
    about and the wall it is held to cannot drift.

    Recovery is always a re-mint; whether it also needs a re-upload depends on timing. The object
    keys are derived from the Run and the artifact names, so they survive a re-mint of the same
    declaration — but the upload reaper collects a lapsed window's uncommitted objects within a
    sweep, so an agent that does not re-mint promptly should expect to re-upload as well.
    """
    return ToolResponse.failure(
        run_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail="the build upload window has expired; re-mint it and finalize again",
        suggested_next_actions=[CREATE_RUN_UPLOAD_TOOL],
        data={
            "reason": "upload_window_expired",
            **upload_expiry_contract(exc.stamp, remint_tool=CREATE_RUN_UPLOAD_TOOL),
        },
    )


_WINDOW_GONE_DETAIL = {
    # Every "the window you were finalizing is not there any more" rejection routes to the one
    # call that re-opens one (ADR-0448). `no_upload_manifest` is the *more common* post-expiry
    # landing — the reaper fires on the same `deadline < now()` the expiry rejection does — so
    # leaving it bare, as it was, stranded the majority case.
    "no_upload_manifest": "this Run has no open upload window; mint one and upload before "
    "finalizing",
    "upload_window_replaced": "the upload window this finalize validated was replaced by a "
    "re-mint; upload against the current window and finalize again",
}


def _complete_envelope(
    run_id: UUID,
    result: BuildStepResult,
    *,
    warning: dict[str, JsonValue] | None = None,
    nudge: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    data: dict[str, JsonValue] = {}
    actions = ["runs.get"]
    if warning is not None:
        data["missing_boot_config"] = warning
        actions = [FEATURE_CONFIG_REQUIREMENTS_TOOL, "runs.get"]
    elif nudge is not None:
        data["missing_effective_config"] = nudge
        actions = [CREATE_RUN_UPLOAD_TOOL, "runs.get"]
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=actions, refs=result.refs(), data=data
    )


def _normalize_cmdline(cmdline: str | None) -> str | None:
    if cmdline is None:
        return None
    cmdline = cmdline.strip()
    return cmdline or None


__all__ = [
    "CompleteBuildHandlers",
    "CompleteBuildValidation",
    "ExternalBuildStore",
    "ObjectStoreFactory",
]
