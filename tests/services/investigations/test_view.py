"""Direct unit tests for the Investigation read-model helpers."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection

from kdive.domain.capacity.state import InvestigationState
from kdive.domain.lifecycle.records import Investigation
from kdive.services.investigations.view import (
    InvestigationRowError,
    attached_runs_and_systems,
    attachments_for_investigations,
    investigation_list_item,
)


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object) -> None:
        return None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


def _conn(rows: list[tuple[object, ...]]) -> AsyncConnection:
    return cast(AsyncConnection, _FakeConn(rows))


def _valid_row() -> dict[str, object]:
    inv = Investigation(
        id=uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        principal="p",
        agent_session="s",
        project="proj",
        title="t",
        description=None,
        external_refs=[],
        state=InvestigationState.OPEN,
    )
    return inv.model_dump()


def test_valid_row_becomes_an_investigation() -> None:
    item = investigation_list_item(_valid_row())
    assert isinstance(item, Investigation)


def test_invalid_row_degrades_to_row_error_carrying_its_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The :86 degrade edge folded in by #1304: a row that fails validation is not raised.
    with caplog.at_level(logging.WARNING, logger="kdive.services.investigations.view"):
        item = investigation_list_item({"id": "not-a-uuid"})
    assert isinstance(item, InvestigationRowError)
    assert item.object_id == "not-a-uuid"
    [record] = [r for r in caplog.records if r.name == "kdive.services.investigations.view"]
    # The degrade warning names the offending id and carries the validation traceback.
    assert record.getMessage() == (
        "investigation not-a-uuid violates the response invariant; degraded"
    )
    assert record.exc_info is not None


def test_row_without_id_degrades_with_none_object_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="kdive.services.investigations.view"):
        item = investigation_list_item({})
    assert isinstance(item, InvestigationRowError)
    assert item.object_id is None
    [record] = [r for r in caplog.records if r.name == "kdive.services.investigations.view"]
    # A row with no id logs the "<missing>" sentinel, not a bare None.
    assert record.getMessage() == (
        "investigation <missing> violates the response invariant; degraded"
    )


def test_attached_runs_and_systems_dedupes_systems_preserving_order() -> None:
    sys_a, sys_b = uuid4(), uuid4()
    run1, run2, run3 = uuid4(), uuid4(), uuid4()
    rows: list[tuple[object, ...]] = [(run1, sys_a), (run2, sys_a), (run3, sys_b)]
    run_ids, system_ids = asyncio.run(attached_runs_and_systems(_conn(rows), uuid4()))
    assert run_ids == [str(run1), str(run2), str(run3)]
    assert system_ids == [str(sys_a), str(sys_b)]


def test_attachments_for_empty_ids_short_circuits() -> None:
    assert asyncio.run(attachments_for_investigations(_conn([]), [])) == {}


def test_attachments_group_runs_and_dedupe_systems_per_investigation() -> None:
    inv = uuid4()
    sys_a = uuid4()
    run1, run2 = uuid4(), uuid4()
    rows: list[tuple[object, ...]] = [(inv, run1, sys_a), (inv, run2, sys_a)]
    result = asyncio.run(attachments_for_investigations(_conn(rows), [inv]))
    assert result[inv]["runs"] == [str(run1), str(run2)]
    assert result[inv]["systems"] == [str(sys_a)]


def test_attachments_seed_every_requested_investigation() -> None:
    present, absent = uuid4(), uuid4()
    run1, sys_a = uuid4(), uuid4()
    rows: list[tuple[object, ...]] = [(present, run1, sys_a)]
    result = asyncio.run(attachments_for_investigations(_conn(rows), [present, absent]))
    assert result[absent] == {"runs": [], "systems": []}
    assert set(result) == {present, absent}
    assert isinstance(absent, UUID)
