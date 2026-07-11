"""Platform audit MCP tool registration."""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops.audit import audit, tool_trail


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the platform audit read tools on ``app``, bound to ``pool``."""
    audit.register(app, pool)
    tool_trail.register(app, pool)
