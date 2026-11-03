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
    CapabilityRequirement,
    DebugRuntimeResolver,
    _EngineOp,
    _gdbmi_maturity,
    _op_audit,
    run_engine_op_with_resolver,
    with_halt_bounds,
)
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine, GdbStopRecord
from kdive.providers.shared.debug_common.gdbmi.policy.capabilities import DebugCapability
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


# The modes that single-step the vCPU; a target without that capability refuses them before any
# resume (ADR-0712). `out` runs to a return breakpoint instead.
_SINGLE_STEP_MODES = frozenset({"into", "over", "instruction"})


def _advance_requirement(mode: str) -> CapabilityRequirement | None:
    if mode not in _SINGLE_STEP_MODES:
        return None
    return CapabilityRequirement(
        capability=DebugCapability.SINGLE_STEP,
        code="single_step_unsupported",
        detail=(
            "this target cannot single-step the vCPU; use debug.advance mode='out', or set a "
            "breakpoint and debug.continue"
        ),
        next_actions=("debug.advance", "debug.set_breakpoint", "debug.continue"),
        data={"mode": mode},
    )


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


_STOPPED_NEXT_ACTIONS = ["debug.read_registers", "debug.read_memory", "debug.list_breakpoints"]
# A background run ends with an interrupt or a waiting continue; every other tool refuses until
# then (#3244).
_RUNNING_NEXT_ACTIONS = ["debug.interrupt", "debug.continue"]


def _continue_op(session_id: str, timeout_sec: float, wait: bool = True) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        if wait:
            stop = engine.continue_(attachment, timeout_sec=timeout_sec)
        else:
            stop = engine.continue_background(attachment)
            if stop is None:
                return ToolResponse.success(
                    session_id, "running", suggested_next_actions=_RUNNING_NEXT_ACTIONS
                )
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=_STOPPED_NEXT_ACTIONS,
            data=_stop_data(stop.reason, stop.timed_out),
        )

    return op


def _interrupt_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        halt = engine.interrupt(attachment)
        data = _stop_data(halt.stop.reason if halt.stop is not None else None, False)
        data["halted_by"] = halt.halted_by
        return ToolResponse.success(
            session_id,
            "stopped",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data=data,
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


def _wait_field() -> object:
    return Field(
        description=(
            "True (default) waits for a stop event. False resumes and returns status 'running' "
            "at once, leaving the guest running after the call; timeout_sec is then ignored."
        )
    )


def _register_debug_continue(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.continue",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    @with_halt_bounds
    async def debug_continue(
        session_id: Annotated[str, _session_id_field("continue execution on")],
        timeout_sec: Annotated[float, _timeout_field()] = 0.0,
        wait: Annotated[bool, _wait_field()] = True,
    ) -> ToolResponse:
        """Resume a live DebugSession. Requires contributor. With wait=True (default) it waits
        for a stop event and returns status 'stopped' with the stop's reason; a wait that ends
        without one interrupts the guest back and returns timed_out=True, so the call can hold
        the session for up to {continue_hold_s} seconds. With wait=False it resumes and returns
        status 'running' at once, leaving the guest running after the call, or 'stopped' if the
        resume already reported a stop. While the guest runs, every other debug tool except
        debug.interrupt and debug.continue refuses with debug_attach_failure code
        inferior_running and sends nothing to the guest. A stop that ends the run (for example a
        breakpoint) is reported only by the next debug.interrupt or debug.continue with
        wait=True, which waits for that stop without resuming again; debug.continue with
        wait=False on a running guest returns 'running' again. Another call on the same session,
        debug.interrupt included, waits while a wait=True continue is in progress; use
        wait=False when you need to break in. A transport_stall failure leaves the guest's state
        unknown: call debug.interrupt again or debug.end_session. If gdb refuses the
        interrupt-back the call fails with debug_attach_failure code interrupt_refused; end the
        session."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _continue_op(session_id, timeout_sec, wait),
            audit=_op_audit("debug.continue", timeout_sec=timeout_sec, wait=wait),
            while_running=True,
        )


def _register_debug_interrupt(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.interrupt",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    @with_halt_bounds
    async def debug_interrupt(
        session_id: Annotated[str, _session_id_field("interrupt")],
    ) -> ToolResponse:
        """Halt the guest of a live DebugSession and report what halted it. Requires
        contributor. Returns status 'stopped' with data.halted_by: 'interrupt' when this call's
        interrupt halted a running guest (data.reason 'signal-received'); 'stop_event' when the
        run had already stopped, or stopped for another cause, such as a breakpoint (data.reason
        names it); 'attach' when the guest was halted by gdb attaching to it on this session's
        first debug call; 'already_halted' when nothing was running. 'attach' and
        'already_halted' send nothing to the guest and return at once. The guest runs between
        calls only after debug.continue with wait=False. If a running guest reports no stop the
        call fails with infrastructure_failure code transport_stall after up to {halt_s} seconds
        and the guest's state is unknown; call debug.interrupt again or debug.end_session. If
        gdb refuses the interrupt the call fails with debug_attach_failure code
        interrupt_refused; end the session."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _interrupt_op(session_id),
            audit=_op_audit("debug.interrupt"),
            while_running=True,
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
        within the wait interrupts back with timed_out=True. Where the gdbstub cannot single-step
        (ppc64le under KVM), 'into', 'over' and 'instruction' return not_implemented with
        code single_step_unsupported before the target resumes; retrying the same mode does not
        succeed, so use 'out' or a breakpoint with debug.continue."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _advance_op(session_id, mode, timeout_sec),
            audit=_op_audit(_ADVANCE_TOOL, f"advance:{mode}", mode=mode, timeout_sec=timeout_sec),
            requires=_advance_requirement(mode),
        )
