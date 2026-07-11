"""Memory, register, and symbol debug-op registration."""

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
from kdive.providers.shared.debug_common.gdbmi.core.engine import MAX_MEMORY_READ_BYTES


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_read_memory(app, pool, runtime)
    _register_debug_read_registers(app, pool, runtime)
    _register_debug_resolve_symbol(app, pool, runtime)


def _read_memory_op(session_id: str, address: int, byte_count: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        blob = engine.read_memory(attachment, address=address, byte_count=byte_count)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.continue"],
            data={
                "address": f"0x{address:x}",
                "byte_count": len(blob),
                "memory_hex": blob.hex(),
            },
        )

    return op


def _read_registers_op(session_id: str, registers: list[str]) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        values = engine.read_registers(attachment, registers)
        rendered = {str(k): str(v) for k, v in values.items()}
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_memory", "debug.continue"],
            data=rendered,
        )

    return op


def _resolve_symbol_op(session_id: str, name: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        address = engine.resolve_symbol(attachment, name)
        return ToolResponse.success(
            session_id,
            "resolved",
            suggested_next_actions=["debug.read_memory", "debug.read_registers"],
            data={"symbol": name, "address": f"0x{address:x}"},
        )

    return op


def _register_debug_read_memory(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_memory",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_memory(
        session_id: Annotated[str, Field(description="The live DebugSession to read memory from.")],
        address: Annotated[int, Field(description="Start address (integer) to read from.")],
        byte_count: Annotated[
            int, Field(description=f"Number of bytes to read (capped at {MAX_MEMORY_READ_BYTES}).")
        ],
    ) -> ToolResponse:
        """Read raw memory bytes from a live DebugSession (bounded by byte_count). Contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_memory_op(session_id, address, byte_count),
            audit=_op_audit("debug.read_memory", address=f"0x{address:x}", byte_count=byte_count),
        )


def _register_debug_read_registers(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.read_registers",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_read_registers(
        session_id: Annotated[
            str, Field(description="The live DebugSession to read registers from.")
        ],
        registers: Annotated[
            list[str],
            Field(description='Register names to read (e.g. ["rip", "rsp"]).'),
        ],
    ) -> ToolResponse:
        """Read named registers from a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _read_registers_op(session_id, registers),
        )


def _register_debug_resolve_symbol(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.resolve_symbol",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_resolve_symbol(
        session_id: Annotated[
            str, Field(description="The live DebugSession to resolve the symbol on.")
        ],
        name: Annotated[
            str,
            Field(
                description="Bare C global or function symbol name to resolve to its address "
                "(e.g. 'd_hash_shift'). Read its value with debug.read_memory. This resolves an "
                "address only; to read a struct field or array member by name "
                "(some_struct->field[3].member), use the drgn path introspect.script instead."
            ),
        ],
    ) -> ToolResponse:
        """Resolve a kernel symbol to its address on a live DebugSession. Requires contributor."""
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _resolve_symbol_op(session_id, name),
        )
