"""Registrar for gdb-MI debug operation tools."""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.debug.operations.runtime import DebugRuntimeResolver


def _register_debug_ops(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugRuntimeResolver
) -> None:
    """Register the gdb-MI `debug.*` tools on ``app``, sharing ``runtime``."""
    from kdive.mcp.tools.debug.operations import (
        breakpoints,
        execution,
        memory,
        modules,
        stack,
        watchpoints,
    )

    breakpoints.register(app, pool, runtime)
    memory.register(app, pool, runtime)
    execution.register(app, pool, runtime)
    stack.register(app, pool, runtime)
    watchpoints.register(app, pool, runtime)
    modules.register(app, pool, runtime)
