"""Watchpoint debug-op registration."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.operations import (
    DebugRuntimeResolver,
    _EngineOp,
    _gdbmi_maturity,
    _op_audit,
    run_engine_op_with_resolver,
)
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine
from kdive.serialization import JsonValue


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_set_watchpoint(app, pool, runtime)
    _register_debug_list_watchpoints(app, pool, runtime)
    _register_debug_clear_watchpoint(app, pool, runtime)


def _set_watchpoint_op(
    session_id: str, symbol: str | None, address: int | None, byte_count: int
) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_watchpoint(
            attachment, symbol=symbol, address=address, byte_count=byte_count
        )
        data: dict[str, JsonValue] = {"number": ref.number, "byte_count": byte_count}
        if ref.expr is not None:
            data["expr"] = ref.expr
        return ToolResponse.success(
            session_id,
            "watching",
            suggested_next_actions=["debug.continue", "debug.list_watchpoints"],
            data=data,
        )

    return op


def _list_watchpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_watchpoints(attachment)
        watchpoints: list[JsonValue] = [
            ref.model_dump(mode="json", exclude_none=True) for ref in refs
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_watchpoint", "debug.continue"],
            data={"count": len(watchpoints), "watchpoints": watchpoints},
        )

    return op


def _clear_watchpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_watchpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_watchpoints"]
        )

    return op


def _register_debug_set_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.set_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_set_watchpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession to set a watchpoint on.")
        ],
        symbol: Annotated[
            str | None,
            Field(description="Bare C symbol to watch for writes (or use address)."),
        ] = None,
        address: Annotated[
            int | None,
            Field(description="Start address (integer) to watch for writes (or use symbol)."),
        ] = None,
        byte_count: Annotated[
            int,
            Field(description="Bytes to watch; one of 1, 2, 4, or 8 (one hardware watchpoint)."),
        ] = 8,
    ) -> ToolResponse:
        """Set a hardware write watchpoint on a symbol/address for a live DebugSession.

        Watchpoints are hardware (debug-register) watchpoints: the stub may accept one yet never
        trap, surfacing as a debug.continue timeout rather than an error. Requires contributor.
        """
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_watchpoint_op(session_id, symbol, address, byte_count),
            audit=_op_audit(
                "debug.set_watchpoint",
                symbol=symbol,
                address=None if address is None else f"0x{address:x}",
                byte_count=byte_count,
            ),
        )


def _register_debug_list_watchpoints(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_watchpoints",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_watchpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoints to list.")
        ],
    ) -> ToolResponse:
        """List all watchpoints on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool, current_context(), session_id, runtime, _list_watchpoints_op(session_id)
        )


def _register_debug_clear_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.clear_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_clear_watchpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoint to clear.")
        ],
        number: Annotated[
            str, Field(description="Watchpoint number to clear (from debug.list_watchpoints).")
        ],
    ) -> ToolResponse:
        """Clear a watchpoint by number on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_watchpoint_op(session_id, number),
            audit=_op_audit("debug.clear_watchpoint", number=number),
        )
