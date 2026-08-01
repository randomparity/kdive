"""`runs.complete_build` MCP handler."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.upload_manifest import UPLOAD_WINDOW_EXPIRED
from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_provenance import external_source_provenance
from kdive.kernel_config.gate import missing_effective_config_nudge, rootfs_mount_warning
from kdive.log import bind_context
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    upload_expiry_contract,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue
from kdive.services.runs.complete_build import (
    NO_UPLOAD_MANIFEST,
    UPLOAD_WINDOW_REPLACED,
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
            return _remint_error(
                run_id,
                "the build upload window has expired; re-mint it and finalize again",
                {
                    "reason": UPLOAD_WINDOW_EXPIRED,
                    **upload_expiry_contract(exc.stamp, remint_tool=CREATE_RUN_UPLOAD_TOOL),
                },
            )
        except CompleteBuildConfigurationError as exc:
            detail = _WINDOW_GONE_DETAIL.get(str(exc.data.get("reason")))
            if detail is not None:
                return _remint_error(run_id, detail, exc.data)
            return _config_error(run_id, data=exc.data)
        except CompleteBuildValidationError as exc:
            response = ToolResponse.failure_from_error(
                run_id,
                exc.error,
                suggested_next_actions=[CREATE_RUN_UPLOAD_TOOL],
            )
            response.refs["external_build_contract"] = EXTERNAL_BUILD_CONTRACT_URI
            return response
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
        async with conn.cursor() as cur:
            await cur.execute("SELECT clock_timestamp()")
            row = await cur.fetchone()
        if row is None:  # Invariant: SELECT clock_timestamp() returns exactly one row.
            raise RuntimeError("SELECT clock_timestamp() returned no row")
        return _complete_envelope(
            uid, result, server_time=row[0].isoformat(), warning=warning, nudge=nudge
        )


def _remint_error(run_id: str, detail: str, data: dict[str, JsonValue]) -> ToolResponse:
    """Reject a finalize whose upload window is gone, pointing at the one call that re-opens one.

    The single envelope for all three of those rejections (ADR-0448), so the category and the
    recovery action cannot drift apart between them. The expiry variant carries the mint's own
    `upload_expiry_contract` fields on top, which is what makes it self-correcting: the wall the
    agent was told about and the wall it is held to are rendered by one function. Recovery is
    always a re-mint and usually also a re-upload — see the `runs.complete_build` docstring, which
    is the copy agents actually read.
    """
    return ToolResponse.failure(
        run_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=[CREATE_RUN_UPLOAD_TOOL],
        data=data,
    )


_WINDOW_GONE_DETAIL = {
    # `no_upload_manifest` is the *more common* post-expiry landing — the reaper fires on the same
    # `deadline < now()` the expiry rejection does — so leaving it bare, as it was, stranded the
    # majority case. Keyed off the service's own constants: a rename there is then an import error
    # here, not a silent downgrade to a bare `configuration_error`.
    NO_UPLOAD_MANIFEST: "this Run has no open upload window; mint one and upload before finalizing",
    UPLOAD_WINDOW_REPLACED: "the upload window this finalize validated was replaced by a "
    "re-mint; upload against the current window and finalize again",
}


def _complete_envelope(
    run_id: UUID,
    result: BuildStepResult,
    *,
    server_time: str,
    warning: dict[str, JsonValue] | None = None,
    nudge: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    data: dict[str, JsonValue] = {"server_time": server_time}
    if result.build_ref is not None:
        data["build_ref"] = result.build_ref
    if result.expires_at is not None:
        data["expires_at"] = result.expires_at
    actions = ["runs.get"]
    refs = dict(result.refs())
    if warning is not None:
        data["missing_boot_config"] = warning
        refs["external_build_contract"] = EXTERNAL_BUILD_CONTRACT_URI
    elif nudge is not None:
        data["missing_effective_config"] = nudge
        actions = [CREATE_RUN_UPLOAD_TOOL, "runs.get"]
        refs["external_build_contract"] = EXTERNAL_BUILD_CONTRACT_URI
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=actions, refs=refs, data=data
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
