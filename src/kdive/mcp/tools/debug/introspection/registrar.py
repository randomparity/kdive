"""FastMCP registration for debug introspection tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools.debug.introspection.common import _OFFLINE_VMCORE, _require_introspection
from kdive.mcp.tools.debug.introspection.live import (
    _DEFAULT_SCRIPT_TIMEOUT,
    _TIMEOUT_FLOOR,
    introspect_run,
    introspect_script,
)
from kdive.mcp.tools.debug.introspection.offline import introspect_from_vmcore
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `introspect.from_vmcore`, `introspect.run`, and `introspect.script` tools."""

    @app.tool(
        name="introspect.from_vmcore",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def introspect_from_vmcore_tool(
        run_id: Annotated[
            str,
            Field(
                description=(
                    "The Run whose captured core to introspect with operator-provided drgn."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run offline drgn introspection over a Run's captured core; returns redacted report."""
        ctx = current_context()

        async def _gated(runtime: ProviderRuntime) -> ToolResponse:
            denied = _require_introspection(run_id, runtime, _OFFLINE_VMCORE)
            if denied is not None:
                return denied
            return await introspect_from_vmcore(
                pool, ctx, run_id=run_id, introspector=runtime.vmcore_introspector
            )

        return await with_runtime_for_run(
            pool, resolver, ctx, run_id, _gated, required_role=Role.VIEWER
        )

    @app.tool(
        name="introspect.run",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def introspect_run_tool(
        session_id: Annotated[str, Field(description="A live drgn-live DebugSession.")],
        helper: Annotated[
            str,
            Field(
                description=(
                    "In-tree drgn helper to run with operator-provided drgn: tasks, modules, "
                    "or sysinfo."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run live drgn introspection over a live drgn-live DebugSession. Requires contributor.

        Returns `data.report` (the helper's output, keyed by helper name), `data.truncated`
        (bool, whether the output was byte-capped), and `data.transcript_sensitivity`. If the
        guest is missing debuginfo, `data.missing_debuginfo` is added as a non-fatal warning.
        """
        return await introspect_run(
            pool,
            current_context(),
            session_id=session_id,
            helper=helper,
            resolver=resolver,
            secret_registry=secret_registry,
        )

    @app.tool(
        name="introspect.script",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def introspect_script_tool(
        session_id: Annotated[str, Field(description="A live drgn-live DebugSession.")],
        script: Annotated[
            str,
            Field(
                description=(
                    "A drgn (Python) script run against the live guest kernel; `prog` is the live "
                    "drgn.Program, already bound as a global in the script's namespace - do not "
                    "`from drgn import prog` (it will fail; there is no such importable name). "
                    "Example: `for t in prog.threads(): print(t.pid)`. Its stdout is returned "
                    "(byte-capped). Each call is a fresh drgn process - put any multi-step work "
                    "in one script."
                )
            ),
        ],
        timeout_sec: Annotated[
            float,
            Field(
                description=(
                    f"In-guest execution bound (seconds); clamped to [{_TIMEOUT_FLOOR}, operator "
                    f"ceiling]. Defaults to {int(_DEFAULT_SCRIPT_TIMEOUT)}. A wedged script is "
                    "recovered with debug.end_session."
                )
            ),
        ] = _DEFAULT_SCRIPT_TIMEOUT,
    ) -> ToolResponse:
        """Run a caller drgn script over a live drgn-live DebugSession. Requires contributor.

        Returns `data.output` (the script's captured stdout, byte-capped), `data.truncated`
        (bool, whether the output was byte-capped), and `data.transcript_sensitivity`. If the
        guest is missing debuginfo, `data.missing_debuginfo` is added as a non-fatal warning.
        """
        return await introspect_script(
            pool,
            current_context(),
            session_id=session_id,
            script=script,
            timeout_sec=timeout_sec,
            resolver=resolver,
            secret_registry=secret_registry,
        )
