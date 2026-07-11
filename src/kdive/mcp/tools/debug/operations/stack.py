"""Stack and disassembly debug-op registration."""

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
    run_engine_op_with_resolver,
)
from kdive.providers.ports.debug import GdbMiAttachment, GdbMiEngine
from kdive.serialization import JsonValue


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_backtrace(app, pool, runtime)
    _register_debug_read_frame(app, pool, runtime)
    _register_debug_disassemble(app, pool, runtime)


def _backtrace_op(session_id: str, max_frames: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.backtrace(attachment, max_frames=max_frames)
        frames: list[JsonValue] = [
            frame.model_dump(mode="json", exclude_none=True) for frame in result.frames
        ]
        return ToolResponse.success(
            session_id,
            "walked",
            suggested_next_actions=["debug.read_frame", "debug.read_registers"],
            data={"frame_count": len(frames), "truncated": result.truncated, "frames": frames},
        )

    return op


def _read_frame_op(session_id: str, level: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        frame = engine.read_frame(attachment, level=level)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.read_memory"],
            data={"level": level, "frame": frame.model_dump(mode="json", exclude_none=True)},
        )

    return op


def _disassemble_op(
    session_id: str, symbol: str | None, address: int | None, instruction_count: int
) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.disassemble(
            attachment, symbol=symbol, address=address, instruction_count=instruction_count
        )
        instructions: list[JsonValue] = [
            insn.model_dump(mode="json", exclude_none=True) for insn in result.instructions
        ]
        return ToolResponse.success(
            session_id,
            "disassembled",
            suggested_next_actions=["debug.read_memory", "debug.read_registers"],
            data={
                "instruction_count": len(instructions),
                "truncated": result.truncated,
                "instructions": instructions,
            },
        )

    return op


def _register_debug_backtrace(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.backtrace",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_backtrace(
        session_id: Annotated[
            str, Field(description="The live DebugSession to walk the stopped stack on.")
        ],
        max_frames: Annotated[
            int,
            Field(
                description="Maximum frames to return (1-64); the backtrace is truncated past it."
            ),
        ] = 64,
    ) -> ToolResponse:
        """Walk the stopped kernel's stack on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _backtrace_op(session_id, max_frames),
        )


def _register_debug_read_frame(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_frame",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_frame(
        session_id: Annotated[
            str, Field(description="The live DebugSession to inspect a stack frame on.")
        ],
        level: Annotated[
            int,
            Field(description="Stack frame index to inspect (0 is the innermost frame)."),
        ],
    ) -> ToolResponse:
        """Inspect one selected stack frame on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_frame_op(session_id, level),
        )


def _register_debug_disassemble(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.disassemble",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_disassemble(
        session_id: Annotated[str, Field(description="The live DebugSession to disassemble on.")],
        symbol: Annotated[
            str | None,
            Field(
                description="Bare C function/symbol name to disassemble around (or use address)."
            ),
        ] = None,
        address: Annotated[
            int | None,
            Field(description="Start address (integer) to disassemble from (or use symbol)."),
        ] = None,
        instruction_count: Annotated[
            int,
            Field(description="Instructions to return (1-256); the window is truncated past it."),
        ] = 64,
    ) -> ToolResponse:
        """Disassemble a bounded window around a symbol/address on a live DebugSession.

        Requires contributor.
        """
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _disassemble_op(session_id, symbol, address, instruction_count),
        )
