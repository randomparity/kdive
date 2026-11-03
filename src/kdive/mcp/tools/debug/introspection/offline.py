"""Offline vmcore introspection handler."""

from __future__ import annotations

from functools import partial
from typing import cast

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.vmcore_retention import await_vmcore_io, vmcore_retention_view
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.vmcore._vmcore_targets import (
    resolve_run_vmcore_target,
    vmcore_target_failure,
)
from kdive.providers.ports.retrieve import VmcoreIntrospector
from kdive.security.authz.context import RequestContext


async def introspect_from_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    introspector: VmcoreIntrospector,
) -> ToolResponse:
    with bind_context(principal=ctx.principal):
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        try:
            async with pool.connection() as conn, conn.transaction():
                await resolve_run_vmcore_target(conn, ctx, run_id)
                async with advisory_xact_lock(conn, LockScope.RUN, uid):
                    resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
                    output = await await_vmcore_io(
                        conn,
                        partial(
                            introspector.from_vmcore,
                            vmcore_ref=resolved.vmcore_ref,
                            debuginfo_ref=resolved.debuginfo_ref,
                            debuginfo_version_id=resolved.debuginfo_version_id,
                            expected_build_id=resolved.build_id,
                        ),
                    )
                    response = ToolResponse.success(
                        run_id,
                        "succeeded",
                        suggested_next_actions=["introspect.from_vmcore", "artifacts.list"],
                        data=cast(
                            ResponseData,
                            {
                                "report": {
                                    "tasks": output.tasks,
                                    "modules": output.modules,
                                    "sysinfo": output.sysinfo,
                                },
                                "truncated": output.truncated,
                                "vmcore_retention": await vmcore_retention_view(conn, uid),
                            },
                        ),
                    )
            return response
        except CategorizedError as exc:
            return vmcore_target_failure(run_id, exc)
        except psycopg.Error:
            return ToolResponse.failure_from_error(
                run_id,
                CategorizedError(
                    "vmcore analysis lost database authority; retry the read",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                ),
            )
