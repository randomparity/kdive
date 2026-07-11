"""Kernel module debug-op registration."""

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
from kdive.providers.shared.debug_common.gdbmi.engine import MAX_MODULES
from kdive.serialization import JsonValue


def register(app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver) -> None:
    _register_debug_list_modules(app, pool, runtime)
    _register_debug_load_module_symbols(app, pool, runtime)


def _list_modules_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.list_modules(attachment, max_modules=MAX_MODULES)
        modules: list[JsonValue] = [
            module.model_dump(mode="json", exclude_none=True) for module in result.modules
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.load_module_symbols", "debug.backtrace"],
            data={
                "count": len(modules),
                "truncated": result.truncated,
                "decode_errors": result.decode_errors,
                "modules": modules,
            },
        )

    return op


def _load_module_symbols_op(session_id: str, module: str, expected_base: int | None) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.load_module_symbols(attachment, module=module, expected_base=expected_base)
        data: dict[str, JsonValue] = {
            "module": result.name,
            "base_address": result.base_address,
            "symbols_loaded": result.symbols_loaded,
        }
        if result.identity_verified is not None:
            data["identity_verified"] = result.identity_verified
        return ToolResponse.success(
            session_id,
            "loaded",
            suggested_next_actions=[
                "debug.backtrace",
                "debug.disassemble",
                "debug.list_modules",
            ],
            data=data,
        )

    return op


def _register_debug_list_modules(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_modules",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_modules(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose loaded modules to list.")
        ],
    ) -> ToolResponse:
        """List loaded kernel modules (name, base address, whether symbols are loaded).

        Requires contributor.
        """
        return await run_engine_op_with_resolver(
            pool, current_context(), session_id, runtime, _list_modules_op(session_id)
        )


def _register_debug_load_module_symbols(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.load_module_symbols",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_load_module_symbols(
        session_id: Annotated[
            str, Field(description="The live DebugSession to load module symbols on.")
        ],
        module: Annotated[
            str,
            Field(description="Loaded module name to load symbols for (from debug.list_modules)."),
        ],
        expected_base: Annotated[
            int | None,
            Field(
                description="The base address seen in debug.list_modules; if it no longer matches "
                "the live module, the load is refused as stale rather than loading wrong symbols."
            ),
        ] = None,
    ) -> ToolResponse:
        """Load one loaded module's symbols at its current base on a live DebugSession.

        Requires contributor.
        """
        return await run_engine_op_with_resolver(
            pool,
            current_context(),
            session_id,
            runtime,
            _load_module_symbols_op(session_id, module, expected_base),
            audit=_op_audit(
                "debug.load_module_symbols",
                module=module,
                expected_base=None if expected_base is None else f"0x{expected_base:x}",
            ),
        )
