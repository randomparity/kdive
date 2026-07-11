"""FastMCP registration for the `vmcore.*` / `postmortem.*` tools (ADR-0031)."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capture import CaptureMethod
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.lifecycle.vmcore.handlers import VmcoreHandlers, list_vmcores
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `vmcore.*` / `postmortem.*` tools on ``app``, bound to ``pool``."""
    handlers = VmcoreHandlers(resolver=resolver, secret_registry=secret_registry)

    @app.tool(
        name="vmcore.fetch",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def vmcore_fetch(
        run_id: Annotated[str, Field(description="The crashed Run whose vmcore to capture.")],
        method: Annotated[
            CaptureMethod | None,
            Field(
                description=(
                    "Core-producing capture method (KDUMP/HOST_DUMP) the bound provider must "
                    "advertise. Omit to resolve the System profile's method; a profile with no "
                    "implicit core method requires an explicit one."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Capture and persist a vmcore."""
        return await handlers.fetch_vmcore(
            pool,
            current_context(),
            run_id=run_id,
            method=method,
            idempotency_key=idempotency_key,
        )

    @app.tool(
        name="vmcore.list",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def vmcore_list(
        run_id: Annotated[
            str,
            Field(description="The Run whose redacted vmcore artifacts to list."),
        ],
    ) -> ToolResponse:
        """List vmcore artifacts for one run."""
        return await list_vmcores(pool, current_context(), run_id=run_id)

    @app.tool(
        name="postmortem.crash",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def postmortem_crash_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to analyze.")],
        commands: Annotated[
            list[str],
            Field(description="Crash commands to run (allowlisted read-only verbs)."),
        ],
    ) -> ToolResponse:
        """Run crash postmortem commands for a captured vmcore."""
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

    @app.tool(
        name="postmortem.triage",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def postmortem_triage_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to triage.")],
    ) -> ToolResponse:
        """Run the default crash triage for a captured vmcore."""
        return await handlers.postmortem_triage(pool, current_context(), run_id=run_id)
