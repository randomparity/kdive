"""The system_devices child ledger: repository, transitions and reclaim (#3697, ADR-0823)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import (
    SYSTEM_DEVICES,
    delete_devices_for_system,
    devices_for_system,
)
from kdive.domain.capacity.state import IllegalTransition, SystemDeviceState
from kdive.domain.lifecycle.records import SystemDevice
from tests.db.test_snapshots_repo import _connect, _seed_system

_DT = datetime(2026, 11, 19, tzinfo=UTC)

_VALID_EDGES = [
    (SystemDeviceState.PLUGGING, SystemDeviceState.ATTACHED),
    (SystemDeviceState.PLUGGING, SystemDeviceState.FAILED),
    (SystemDeviceState.ATTACHED, SystemDeviceState.UNPLUGGING),
    (SystemDeviceState.ATTACHED, SystemDeviceState.LOST),
    (SystemDeviceState.UNPLUGGING, SystemDeviceState.ATTACHED),
    (SystemDeviceState.UNPLUGGING, SystemDeviceState.FAILED),
    (SystemDeviceState.UNPLUGGING, SystemDeviceState.LOST),
    (SystemDeviceState.FAILED, SystemDeviceState.UNPLUGGING),
    (SystemDeviceState.LOST, SystemDeviceState.UNPLUGGING),
]
_INVALID_EDGES = [
    (frm, to)
    for frm in SystemDeviceState
    for to in SystemDeviceState
    if (frm, to) not in _VALID_EDGES
]


def _device(system_id: UUID, **kw: object) -> SystemDevice:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        system_id=system_id,
        kind="nic",
        state=SystemDeviceState.PLUGGING,
        network="default",
    )
    base.update(kw)
    return SystemDevice.model_validate(base)


def test_insert_round_trips_columns_and_lists_oldest_first(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            other = await _seed_system(conn)
            first = await SYSTEM_DEVICES.insert(
                conn,
                _device(
                    sid,
                    identity={"mac": "52:54:00:00:00:01"},
                    detail="d",
                    settle_deadline=_DT,
                    provider_handle="h",
                ),
            )
            await asyncio.sleep(0.01)
            await SYSTEM_DEVICES.insert(conn, _device(sid, kind="disk", network=None, size_gb=5))
            await SYSTEM_DEVICES.insert(conn, _device(other))
            rows = await devices_for_system(conn, sid)
            assert [r.kind for r in rows] == ["nic", "disk"]
            assert rows[0] == first
            assert first.identity == {"mac": "52:54:00:00:00:01"}
            assert rows[1].size_gb == 5
            assert await SYSTEM_DEVICES.get(conn, first.id) == first

    asyncio.run(scenario())


def test_more_than_eight_devices_are_not_a_schema_constraint(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            for _ in range(9):
                await SYSTEM_DEVICES.insert(conn, _device(sid))
            assert len(await devices_for_system(conn, sid)) == 9

    asyncio.run(scenario())


@pytest.mark.parametrize(("frm", "to"), _VALID_EDGES)
def test_valid_edges_pass(migrated_url: str, frm: SystemDeviceState, to: SystemDeviceState) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            row = await SYSTEM_DEVICES.insert(conn, _device(await _seed_system(conn), state=frm))
            assert (await SYSTEM_DEVICES.update_state(conn, row.id, to)).state is to

    asyncio.run(scenario())


@pytest.mark.parametrize(("frm", "to"), _INVALID_EDGES)
def test_invalid_edges_are_refused(
    migrated_url: str, frm: SystemDeviceState, to: SystemDeviceState
) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            row = await SYSTEM_DEVICES.insert(conn, _device(await _seed_system(conn), state=frm))
            with pytest.raises(IllegalTransition):
                await SYSTEM_DEVICES.update_state(conn, row.id, to)
            fetched = await SYSTEM_DEVICES.get(conn, row.id)
            assert fetched is not None and fetched.state is frm

    asyncio.run(scenario())


def test_delete_removes_one_row(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            row = await SYSTEM_DEVICES.insert(conn, _device(await _seed_system(conn)))
            assert await SYSTEM_DEVICES.delete(conn, row.id) is True
            assert await SYSTEM_DEVICES.get(conn, row.id) is None

    asyncio.run(scenario())


def test_delete_devices_for_system_is_scoped_and_counts(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            other = await _seed_system(conn)
            await SYSTEM_DEVICES.insert(conn, _device(sid))
            await SYSTEM_DEVICES.insert(conn, _device(sid, state=SystemDeviceState.FAILED))
            await SYSTEM_DEVICES.insert(conn, _device(other))
            assert await delete_devices_for_system(conn, sid) == 2
            assert await devices_for_system(conn, sid) == []
            assert len(await devices_for_system(conn, other)) == 1
            assert await delete_devices_for_system(conn, sid) == 0

    asyncio.run(scenario())


def test_devices_cascade_when_system_deleted(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            await SYSTEM_DEVICES.insert(conn, _device(sid))
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM systems WHERE id = %s", (sid,))
            assert await devices_for_system(conn, sid) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(("kind", "state"), [("pcie", "plugging"), ("nic", "gone")])
def test_check_constraints_reject_unknown_values(migrated_url: str, kind: str, state: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO system_devices (system_id, kind, state) VALUES (%s, %s, %s)",
                    (sid, kind, state),
                )

    asyncio.run(scenario())
