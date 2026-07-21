"""Execution-control debug-op registration."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.debug.operations.runtime import (
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
    _register_debug_step(app, pool, runtime)
    _register_debug_next(app, pool, runtime)
    _register_debug_step_instruction(app, pool, runtime)
    _register_debug_finish(app, pool, runtime)


# After a single-step or finish the agent inspects where it landed, then steps again or resumes.
_STEP_NEXT_ACTIONS = ["debug.read_registers", "debug.backtrace", "debug.step", "debug.continue"]


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


def _step_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.step(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=_STEP_NEXT_ACTIONS,
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _next_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.next(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=_STEP_NEXT_ACTIONS,
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _step_instruction_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.step_instruction(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=_STEP_NEXT_ACTIONS,
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _finish_op(session_id: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        stop = engine.finish(attachment, timeout_sec=timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=["debug.read_registers", "debug.backtrace", "debug.continue"],
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _session_id_field(verb: str) -> object:
    return Field(description=f"The live DebugSession to {verb}.")


def _timeout_field() -> object:
    return Field(
        description="Seconds to wait for a stop event; 0.0 uses the provider interactive wait cap."
    )


def _register_debug_continue(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.continue",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_continue(
        session_id: Annotated[str, _session_id_field("continue execution on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
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
        session_id: Annotated[str, _session_id_field("interrupt")],
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


def _register_debug_step(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(name="debug.step", annotations=_docmeta.mutating(), meta=_gdbmi_maturity())
    async def debug_step(
        session_id: Annotated[str, _session_id_field("step into calls on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
    ) -> ToolResponse:
        """Step one source line, into called functions, on a live DebugSession, and wait for the
        stop. The target must already be stopped (halt it with debug.interrupt or hit a breakpoint)
        to step from. In a region with no debug symbols this returns timed_out=True or a
        debug_attach_failure ("Cannot find bounds of current function"); use
        debug.step_instruction there. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _step_op(session_id, timeout_sec),
            audit=_op_audit("debug.step", timeout_sec=timeout_sec),
        )


def _register_debug_next(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(name="debug.next", annotations=_docmeta.mutating(), meta=_gdbmi_maturity())
    async def debug_next(
        session_id: Annotated[str, _session_id_field("step over calls on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
    ) -> ToolResponse:
        """Step one source line, over called functions, on a live DebugSession, and wait for the
        stop. The target must already be stopped (halt it with debug.interrupt or hit a breakpoint)
        to step from. Same symbol-poor behavior as debug.step (use debug.step_instruction where the
        current code has no debug symbols). Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _next_op(session_id, timeout_sec),
            audit=_op_audit("debug.next", timeout_sec=timeout_sec),
        )


def _register_debug_step_instruction(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.step_instruction", annotations=_docmeta.mutating(), meta=_gdbmi_maturity()
    )
    async def debug_step_instruction(
        session_id: Annotated[str, _session_id_field("step one instruction on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
    ) -> ToolResponse:
        """Step one machine instruction on a live DebugSession, and wait for the stop. The target
        must already be stopped (halt it with debug.interrupt or hit a breakpoint) to step from.
        Works without debug symbols, so it is the fallback for stepping in symbol-poor regions.
        Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _step_instruction_op(session_id, timeout_sec),
            audit=_op_audit("debug.step_instruction", timeout_sec=timeout_sec),
        )


def _register_debug_finish(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(name="debug.finish", annotations=_docmeta.mutating(), meta=_gdbmi_maturity())
    async def debug_finish(
        session_id: Annotated[str, _session_id_field("resume until the current frame returns on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
    ) -> ToolResponse:
        """Resume a live DebugSession until the current (innermost) frame returns, and wait for
        the stop. The target must already be stopped (halt it with debug.interrupt or hit a
        breakpoint) to resume from. A frame that does not return within the wait interrupts back
        with timed_out=True. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _finish_op(session_id, timeout_sec),
            audit=_op_audit("debug.finish", timeout_sec=timeout_sec),
        )
