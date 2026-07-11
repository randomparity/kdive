"""Breakpoint debug-op registration."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.ops import (
    DebugRuntimeResolver,
    _EngineOp,
    _gdbmi_maturity,
    _op_audit,
    run_engine_op_with_resolver,
)
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_set_breakpoint(app, pool, runtime)
    _register_debug_clear_breakpoint(app, pool, runtime)
    _register_debug_list_breakpoints(app, pool, runtime)


def _set_breakpoint_op(session_id: str, location: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_breakpoint(attachment, location)
        return ToolResponse.success(
            session_id,
            "set",
            suggested_next_actions=["debug.continue", "debug.list_breakpoints"],
            data={"number": ref.number},
        )

    return op


def _clear_breakpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_breakpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_breakpoints"]
        )

    return op


def _list_breakpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_breakpoints(attachment)
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_breakpoint", "debug.continue"],
            data={"count": len(refs)},
        )

    return op


def _register_debug_set_breakpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.set_breakpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_set_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession to set a breakpoint on.")
        ],
        location: Annotated[str, Field(description="Bare C function or symbol name to break at.")],
    ) -> ToolResponse:
        """Set a breakpoint on a live DebugSession via gdb-MI. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_breakpoint_op(session_id, location),
            audit=_op_audit("debug.set_breakpoint", location=location),
        )


def _register_debug_clear_breakpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.clear_breakpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_clear_breakpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoint to clear.")
        ],
        number: Annotated[
            str,
            Field(description="Breakpoint number to clear (from debug.list_breakpoints)."),
        ],
    ) -> ToolResponse:
        """Clear a breakpoint by number on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_breakpoint_op(session_id, number),
            audit=_op_audit("debug.clear_breakpoint", number=number),
        )


def _register_debug_list_breakpoints(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_breakpoints",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_breakpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose breakpoints to list.")
        ],
    ) -> ToolResponse:
        """List all breakpoints on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool, current_context(), session_id, runtime, _list_breakpoints_op(session_id)
        )
