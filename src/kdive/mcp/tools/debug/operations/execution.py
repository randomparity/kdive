"""Execution-control debug-op registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
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
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine, GdbStopRecord
from kdive.serialization import JsonValue

_ADVANCE_TOOL = "debug.advance"

AdvanceMode = Literal["into", "over", "instruction", "out"]

_AdvanceCall = Callable[[GdbMiEngine, GdbMiAttachment, float], GdbStopRecord]

# Each mode dispatches one gdb-MI engine call. All four share a signature, response shape,
# authorization, and annotations, which is what lets them ride one tool (ADR-0463 §2).
_ADVANCE_CALLS: dict[str, _AdvanceCall] = {
    "into": lambda engine, att, timeout: engine.step(att, timeout_sec=timeout),
    "over": lambda engine, att, timeout: engine.next(att, timeout_sec=timeout),
    "instruction": lambda engine, att, timeout: engine.step_instruction(att, timeout_sec=timeout),
    "out": lambda engine, att, timeout: engine.finish(att, timeout_sec=timeout),
}


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_continue(app, pool, runtime)
    _register_debug_interrupt(app, pool, runtime)
    _register_debug_advance(app, pool, runtime)


# After any advance the agent inspects where it landed, then advances again or resumes. One list
# for every mode: an `out` lands in the caller frame, which is as steppable as any other stop
# (ADR-0463 §4).
_ADVANCE_NEXT_ACTIONS = [
    "debug.read_registers",
    "debug.backtrace",
    _ADVANCE_TOOL,
    "debug.continue",
]


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


def _advance_op(session_id: str, mode: str, timeout_sec: float) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        call = _ADVANCE_CALLS.get(mode)
        if call is None:
            # Unreachable through MCP (the schema enum rejects it first); this keeps a direct
            # in-process caller on the failure-envelope path instead of a bare KeyError.
            raise CategorizedError(
                f"unknown advance mode {mode!r}; expected one of {sorted(_ADVANCE_CALLS)}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"field": "mode", "value": mode},
            )
        stop = call(engine, attachment, timeout_sec)
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=_ADVANCE_NEXT_ACTIONS,
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


def _mode_field() -> object:
    return Field(
        description=(
            "How far to advance: 'into' runs one source line and enters called functions; "
            "'over' runs one source line and steps past called functions; 'instruction' runs "
            "one machine instruction and is the fallback where the code has no debug symbols; "
            "'out' resumes until the current (innermost) frame returns, so it needs a frame "
            "that can return."
        )
    )


def _register_debug_advance(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(name=_ADVANCE_TOOL, annotations=_docmeta.mutating(), meta=_gdbmi_maturity())
    async def debug_advance(
        session_id: Annotated[str, _session_id_field("advance execution on")],
        mode: Annotated[AdvanceMode, _mode_field()],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
    ) -> ToolResponse:
        """Advance a stopped live DebugSession by one step and wait for the stop. Requires
        contributor. The target must already be stopped (halt it with debug.interrupt or hit a
        breakpoint) to advance from. mode='into' steps one source line into called functions,
        'over' steps one source line over them, 'instruction' steps one machine instruction, and
        'out' resumes until the current frame returns. In a region with no debug symbols 'into'
        and 'over' return timed_out=True or a debug_attach_failure ("Cannot find bounds of
        current function"); use 'instruction' there. 'out' needs a frame that can return — in the
        outermost frame it fails with debug_attach_failure — and a frame that does not return
        within the wait interrupts back with timed_out=True."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _advance_op(session_id, mode, timeout_sec),
            audit=_op_audit(_ADVANCE_TOOL, f"advance:{mode}", mode=mode, timeout_sec=timeout_sec),
        )
