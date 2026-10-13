"""The harness names the migration-owner DSN before an under-privileged DSN fails opaquely."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.live_stack import scenario
from tests.integration.live_stack.scenario import ScenarioStop
from tests.integration.live_stack.tool_cells import require_snapshot_read_access


class _Cursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[str]]:
        return self._rows


class _Conn:
    """Answers the privilege query with the tables ``denied`` names, in query order."""

    def __init__(self, denied: set[str]) -> None:
        self._denied = denied

    async def execute(self, _query: str, params: tuple[list[str], ...]) -> _Cursor:
        return _Cursor([(t,) for t in params[0] if t in self._denied])


def test_snapshot_preflight_names_each_unreadable_table() -> None:
    conn = _Conn({"investigation_build_use_recoveries", "worker_incarnations"})
    tables = ["jobs", "investigation_build_use_recoveries", "worker_incarnations"]
    with pytest.raises(PermissionError) as raised:
        asyncio.run(require_snapshot_read_access(conn, tables))  # ty: ignore[invalid-argument-type]
    message = str(raised.value)
    assert "investigation_build_use_recoveries, worker_incarnations" in message
    assert "KDIVE_MIGRATION_DATABASE_URL" in message
    assert "server DSN" not in message


def test_snapshot_preflight_passes_a_dsn_that_reads_every_table() -> None:
    asyncio.run(require_snapshot_read_access(_Conn(set()), ["jobs"]))  # ty: ignore[invalid-argument-type]


def test_unset_database_url_names_the_migration_owner_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scenario, "require_issuer", lambda: object())
    monkeypatch.delenv("KDIVE_DATABASE_URL", raising=False)
    with pytest.raises(ScenarioStop) as raised:
        scenario.prerequisites()
    assert "KDIVE_MIGRATION_DATABASE_URL" in str(raised.value)
    assert "export the server DSN" not in str(raised.value)
