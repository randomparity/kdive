"""Offline vmcore introspection handler."""

from __future__ import annotations

import asyncio
from typing import cast

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target, vmcore_target_failure
from kdive.providers.ports.retrieve import VmcoreIntrospector
from kdive.security.authz.context import RequestContext


async def introspect_from_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    introspector: VmcoreIntrospector,
) -> ToolResponse:
    """Run offline drgn introspection over the Run's captured core; return the redacted report.

    Requires the viewer role. A malformed `run_id` is a `configuration_error`; a Run that is
    absent, in an ungranted project (no-leak), or missing its target artifact (no captured core,
    null `debuginfo_ref`, or no recorded `build` step - checked in that order, ADR-0165) is
    `not_found` (ADR-0097). A provenance mismatch or a drgn open/decode fault surfaces as the
    port's typed `CategorizedError` category, never a 500. Off a prepared live host, the provider
    seam reports ``missing_dependency`` instead of importing drgn.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
            except CategorizedError as exc:
                return vmcore_target_failure(run_id, exc)
        try:
            output = await asyncio.to_thread(
                introspector.from_vmcore,
                vmcore_ref=resolved.vmcore_ref,
                debuginfo_ref=resolved.debuginfo_ref,
                expected_build_id=resolved.build_id,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        report = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["introspect.from_vmcore", "artifacts.list"],
            data=cast(
                ResponseData,
                {"report": report, "truncated": output.truncated},
            ),
        )
