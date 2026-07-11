"""Execution-control debug-op registration."""

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
    _register_debug_continue(app, pool, runtime)
    _register_debug_interrupt(app, pool, runtime)


def _continue_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.continue_(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=[
                "debug.read_registers",
                "debug.read_memory",
                "debug.list_breakpoints",
            ],
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _interrupt_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.interrupt(attachment)
        reason = stop.reason if stop is not None else None
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data=_stop_data(reason, False),
        )

    return op


def _stop_data(reason: str | None, timed_out: bool) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"timed_out": timed_out}
    if reason is not None:
        data["reason"] = reason
    return data


def _register_debug_continue(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.continue",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_continue(
        session_id: Annotated[
            str, Field(description="The live DebugSession to continue execution on.")
        ],
        timeout_sec: Annotated[
            float,
            Field(
                description="Seconds to wait for a stop event; 0.0 uses the provider "
                "interactive wait cap."
            ),
        ] = 0.0,
    ) -> ToolResponse:
        """Resume a live DebugSession and wait for a stop event. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _continue_op(session_id, timeout_sec),
            audit=_op_audit("debug.continue", timeout_sec=timeout_sec),
        )


def _register_debug_interrupt(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.interrupt",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_interrupt(
        session_id: Annotated[str, Field(description="The live DebugSession to interrupt.")],
    ) -> ToolResponse:
        """Send an interrupt to halt a running live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _interrupt_op(session_id),
            audit=_op_audit("debug.interrupt"),
        )
