"""Reconciler recovery for stranded plug/un-plug device rows (#3699, ADR-0823).

A `plugging` or `unplugging` row with no active job of its own kind can never settle, so the repair
moves it to `failed`. It runs over the kdive_reconciler login and takes only a connection, so it
cannot reach a provider.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEM_DEVICES
from kdive.domain.capacity.state import JobState, SystemDeviceState
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import JobKind
from kdive.reconciler import loop
from kdive.reconciler.repairs.systems import repair_stalled_device_ops
from tests.conftest import _RoleDsns
from tests.reconciler.conftest import connect, run_repair, seed_system

_DT = datetime(2026, 11, 19, tzinfo=UTC)
_PLUGGING, _UNPLUGGING = SystemDeviceState.PLUGGING, SystemDeviceState.UNPLUGGING


async def _seed_device(conn: psycopg.AsyncConnection, system_id: UUID, state: Any) -> UUID:
    row = await SYSTEM_DEVICES.insert(
        conn,
        SystemDevice.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                system_id=system_id,
                kind="nic",
                state=state,
                settle_deadline=_DT if state in (_PLUGGING, _UNPLUGGING) else None,
            )
        ),
    )
    return row.id


async def _seed_job(
    conn: psycopg.AsyncConnection, kind: JobKind, device_id: UUID, state: JobState
) -> None:
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key) "
        "VALUES (%s, %s, %s, 1, 1, 'w', now() + make_interval(secs => 300), %s, %s)",
        (
            kind.value,
            Jsonb({"system_id": str(uuid4()), "device_id": str(device_id)}),
            state.value,
            Jsonb({"principal": "t", "agent_session": None, "project": "proj"}),
            str(uuid4()),
        ),
    )


async def _repair(dsn: str) -> int:
    async with AsyncConnectionPool(dsn, min_size=1, open=False) as pool:
        await pool.open()
        return await run_repair(pool, repair_stalled_device_ops)


def _run(migrated_url: str, authority_role_dsns: _RoleDsns, scenario: Any) -> None:
    async def main() -> None:
        conn = await connect(migrated_url)
        try:
            system_id = await seed_system(conn)
            await scenario(conn, system_id, authority_role_dsns("kdive_reconciler"))
        finally:
            await conn.close()

    asyncio.run(main())


async def _row(conn: psycopg.AsyncConnection, device_id: UUID) -> SystemDevice:
    row = await SYSTEM_DEVICES.get(conn, device_id)
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (_PLUGGING, JobKind.PLUG_DEVICE),
        (_UNPLUGGING, JobKind.UNPLUG_DEVICE),
        (_UNPLUGGING, JobKind.FORCE_UNPLUG_DEVICE),
    ],
)
@pytest.mark.parametrize(
    "job_state", [None, JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELED]
)
def test_a_stalled_row_with_no_active_job_becomes_failed(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    state: SystemDeviceState,
    kind: JobKind,
    job_state: JobState | None,
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        device_id = await _seed_device(conn, system_id, state)
        if job_state is not None:
            await _seed_job(conn, kind, device_id, job_state)

        assert await _repair(dsn) == 1

        row = await _row(conn, device_id)
        assert row.state is SystemDeviceState.FAILED
        assert row.detail and "worker ended" in row.detail
        assert row.settle_deadline is None

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (_PLUGGING, JobKind.PLUG_DEVICE),
        (_UNPLUGGING, JobKind.UNPLUG_DEVICE),
        (_UNPLUGGING, JobKind.FORCE_UNPLUG_DEVICE),
    ],
)
@pytest.mark.parametrize("job_state", [JobState.QUEUED, JobState.RUNNING])
def test_a_row_with_an_active_job_is_left_alone(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    state: SystemDeviceState,
    kind: JobKind,
    job_state: JobState,
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        device_id = await _seed_device(conn, system_id, state)
        await _seed_job(conn, kind, device_id, job_state)

        assert await _repair(dsn) == 0

        row = await _row(conn, device_id)
        assert row.state is state and row.settle_deadline is not None

    _run(migrated_url, authority_role_dsns, scenario)


def test_only_a_job_of_the_rows_own_kind_counts_as_active(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        plugging = await _seed_device(conn, system_id, _PLUGGING)
        await _seed_job(conn, JobKind.UNPLUG_DEVICE, plugging, JobState.RUNNING)

        assert await _repair(dsn) == 1
        assert (await _row(conn, plugging)).state is SystemDeviceState.FAILED

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_forced_unplug_job_does_not_own_a_plugging_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        plugging = await _seed_device(conn, system_id, _PLUGGING)
        await _seed_job(conn, JobKind.FORCE_UNPLUG_DEVICE, plugging, JobState.RUNNING)

        assert await _repair(dsn) == 1
        assert (await _row(conn, plugging)).state is SystemDeviceState.FAILED

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_plug_job_does_not_own_an_unplugging_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        unplugging = await _seed_device(conn, system_id, _UNPLUGGING)
        await _seed_job(conn, JobKind.PLUG_DEVICE, unplugging, JobState.RUNNING)

        assert await _repair(dsn) == 1
        assert (await _row(conn, unplugging)).state is SystemDeviceState.FAILED

    _run(migrated_url, authority_role_dsns, scenario)


def test_the_stalled_row_detail_names_both_unplug_tools(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        device_id = await _seed_device(conn, system_id, _UNPLUGGING)

        assert await _repair(dsn) == 1

        detail = (await _row(conn, device_id)).detail
        assert detail and "systems.unplug_device" in detail
        assert "systems.force_unplug_device" in detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_job_for_another_device_does_not_cover_the_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        stalled = await _seed_device(conn, system_id, _PLUGGING)
        live = await _seed_device(conn, system_id, _PLUGGING)
        await _seed_job(conn, JobKind.PLUG_DEVICE, live, JobState.RUNNING)

        assert await _repair(dsn) == 1
        assert (await _row(conn, stalled)).state is SystemDeviceState.FAILED
        assert (await _row(conn, live)).state is _PLUGGING

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize(
    "state",
    [SystemDeviceState.ATTACHED, SystemDeviceState.FAILED, SystemDeviceState.LOST],
)
def test_settled_rows_are_never_touched(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemDeviceState
) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        device_id = await _seed_device(conn, system_id, state)

        assert await _repair(dsn) == 0
        assert (await _row(conn, device_id)).state is state

    _run(migrated_url, authority_role_dsns, scenario)


def test_every_stalled_row_is_counted(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    async def scenario(conn: psycopg.AsyncConnection, system_id: UUID, dsn: str) -> None:
        for state in (_PLUGGING, _PLUGGING, _UNPLUGGING):
            await _seed_device(conn, system_id, state)

        assert await _repair(dsn) == 3

    _run(migrated_url, authority_role_dsns, scenario)


def test_the_repair_is_registered_after_abandoned_jobs() -> None:
    kinds = loop.ALL_REPAIR_KINDS

    assert "stalled_device_ops" in kinds
    assert kinds.index("abandoned_jobs") < kinds.index("stalled_device_ops")
