"""`runs.complete_build` MCP handler."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError
from kdive.domain.external_provenance import external_source_provenance
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.catalog.artifacts.expected_uploads import EXPECTED_UPLOADS_TOOL
from kdive.mcp.tools.catalog.artifacts.uploads import CREATE_RUN_UPLOAD_TOOL
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.complete_build import (
    CompleteBuildConfigurationError,
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
            return _complete_envelope(uid, recorded)

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
        except CompleteBuildConfigurationError as exc:
            return _config_error(run_id, data=exc.data)
        except CompleteBuildValidationError as exc:
            return ToolResponse.failure_from_error(
                run_id,
                exc.error,
                suggested_next_actions=[EXPECTED_UPLOADS_TOOL, CREATE_RUN_UPLOAD_TOOL],
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        return _complete_envelope(uid, result)


def _complete_envelope(run_id: UUID, result: BuildStepResult) -> ToolResponse:
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=["runs.get"], refs=result.refs()
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
