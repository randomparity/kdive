"""Direct structural pin for the ops.audit tool registrar."""

from __future__ import annotations

from typing import cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops.audit import registrar


def test_register_delegates_to_both_audit_read_registrars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object, object]] = []

    def _record(name: str):
        def _register(app: object, pool: object) -> None:
            calls.append((name, app, pool))

        return _register

    monkeypatch.setattr(registrar.audit, "register", _record("audit"))
    monkeypatch.setattr(registrar.tool_trail, "register", _record("tool_trail"))

    app = FastMCP("ops-audit-registrar-test")
    pool = cast(AsyncConnectionPool, object())
    registrar.register(app, pool)

    assert calls == [("audit", app, pool), ("tool_trail", app, pool)]
