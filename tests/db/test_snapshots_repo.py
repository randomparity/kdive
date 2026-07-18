"""The snapshots child ledger: repository + query helpers (#1254, ADR-0378)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import (
    ALLOCATIONS,
    RESOURCES,
    SNAPSHOTS,
    SYSTEMS,
    snapshot_by_name,
    snapshots_for_system,
)
from kdive.domain.capacity.state import (
    AllocationState,
    IllegalTransition,
    ResourceStatus,
    SnapshotState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, Snapshot, System

_DT = datetime(2026, 7, 17, tzinfo=UTC)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_system(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="p",
                cost_class="c",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            )
        ),
    )
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=res.id,
                state=AllocationState.REQUESTED,
            )
        ),
    )
    sysm = await SYSTEMS.insert(
        conn,
        System.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile={"k": "v"},
            )
        ),
    )
    return sysm.id


def _snapshot(system_id: UUID, name: str, **kw: object) -> Snapshot:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        system_id=system_id,
        name=name,
        include_memory=True,
        state=SnapshotState.CREATING,
    )
    base.update(kw)
    return Snapshot.model_validate(base)


def test_insert_get_by_name_and_state_machine(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            await SNAPSHOTS.insert(conn, _snapshot(sid, "before-bug"))
            got = await snapshot_by_name(conn, sid, "before-bug")
            assert got is not None and got.state is SnapshotState.CREATING
            promoted = await SNAPSHOTS.update_state(conn, got.id, SnapshotState.AVAILABLE)
            assert promoted.state is SnapshotState.AVAILABLE
            with pytest.raises(IllegalTransition):
                await SNAPSHOTS.update_state(conn, got.id, SnapshotState.CREATING)
            assert await snapshot_by_name(conn, sid, "missing") is None

    asyncio.run(scenario())


def test_list_for_system_is_newest_first(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            await SNAPSHOTS.insert(
                conn, _snapshot(sid, "old", created_at=datetime(2026, 7, 17, 1, tzinfo=UTC))
            )
            await SNAPSHOTS.insert(
                conn, _snapshot(sid, "new", created_at=datetime(2026, 7, 17, 2, tzinfo=UTC))
            )
            rows = await snapshots_for_system(conn, sid)
            assert [r.name for r in rows] == ["new", "old"]

    asyncio.run(scenario())


def test_unique_name_per_system_is_enforced(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            await SNAPSHOTS.insert(conn, _snapshot(sid, "dup"))
            with pytest.raises(psycopg.errors.UniqueViolation):
                await SNAPSHOTS.insert(conn, _snapshot(sid, "dup"))

    asyncio.run(scenario())


def test_snapshots_cascade_when_system_deleted(migrated_url: str) -> None:
    async def scenario() -> None:
        async with await _connect(migrated_url) as conn:
            sid = await _seed_system(conn)
            await SNAPSHOTS.insert(conn, _snapshot(sid, "cp"))
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM systems WHERE id = %s", (sid,))
            assert await snapshots_for_system(conn, sid) == []

    asyncio.run(scenario())
