"""Registrar for gdb-MI debug operation tools."""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.debug.operations.runtime import DebugRuntimeResolver


def _register_debug_ops(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    """Register the gdb-MI `debug.*` tools on ``app``, sharing ``runtime``."""
    from kdive.mcp.tools.debug.operations.breakpoints import register as register_breakpoints
    from kdive.mcp.tools.debug.operations.execution import register as register_execution
    from kdive.mcp.tools.debug.operations.memory import register as register_memory
    from kdive.mcp.tools.debug.operations.modules import register as register_modules
    from kdive.mcp.tools.debug.operations.stack import register as register_stack
    from kdive.mcp.tools.debug.operations.watchpoints import register as register_watchpoints

    register_breakpoints(app, pool, runtime)
    register_memory(app, pool, runtime)
    register_execution(app, pool, runtime)
    register_stack(app, pool, runtime)
    register_watchpoints(app, pool, runtime)
    register_modules(app, pool, runtime)
