"""Direct structural pin for the ops.inventory tool registrar."""

from __future__ import annotations

from typing import cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops.inventory import registrar


def test_register_delegates_to_inventory_read_and_export_registrars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object, object]] = []

    def _record(name: str):
        def _register(app: object, pool: object) -> None:
            calls.append((name, app, pool))

        return _register

    monkeypatch.setattr(registrar.inventory, "register", _record("inventory"))
    monkeypatch.setattr(registrar.inventory_export, "register", _record("inventory_export"))

    app = FastMCP("ops-inventory-registrar-test")
    pool = cast(AsyncConnectionPool, object())
    registrar.register(app, pool)

    assert calls == [("inventory", app, pool), ("inventory_export", app, pool)]
