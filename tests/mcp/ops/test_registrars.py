"""Audit and inventory registrars expose their complete leaf-tool families."""

from __future__ import annotations

from typing import cast

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.tools.ops.audit.registrar import register as register_audit
from kdive.mcp.tools.ops.inventory.registrar import register as register_inventory


def test_audit_registrar_registers_query_and_tool_trail() -> None:
    app = FastMCP("audit-registrar-test")
    register_audit(app, cast(AsyncConnectionPool, object()))
    assert {tool.name for tool in registered_tools(app)} == {"audit.query", "ops.tool_trail"}


def test_inventory_registrar_registers_reads_overrides_and_export() -> None:
    app = FastMCP("inventory-registrar-test")
    register_inventory(app, cast(AsyncConnectionPool, object()))
    assert {tool.name for tool in registered_tools(app)} == {
        "inventory.list",
        "inventory.clear_override",
        "ops.export_systems_toml",
    }
