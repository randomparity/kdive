"""Inventory MCP tool registration."""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops.inventory import inventory, inventory_export


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the inventory read/export tools on ``app``, bound to ``pool``."""
    inventory.register(app, pool)
    inventory_export.register(app, pool)
