"""Worker handlers for snapshot/restore/delete (#1254, ADR-0378).

Drives the handlers directly against a migrated Postgres with a fake Snapshotter, so the
ledger/state transitions are exercised without a live guest.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

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
    JobState,
    ResourceStatus,
    SnapshotState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, Snapshot, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.systems import (
    reprovision_handler,
    restore_handler,
    snapshot_delete_handler,
    snapshot_handler,
    teardown_handler,
)
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from tests.mcp.systems_support import (
    PROVISIONING_PROFILE,
    FakeProvisioning,
    provider_resolver,
)

_DT = datetime(2026, 7, 17, tzinfo=UTC)


class _FakeSnapshotter:
    def __init__(
        self, *, create_error: Exception | None = None, revert_error: Exception | None = None
    ) -> None:
        self.created: list[tuple[str, str, bool]] = []
        self.reverted: list[tuple[str, str, bool]] = []
        self.deleted: list[tuple[str, str]] = []
        self.deleted_all: list[str] = []
        self._create_error = create_error
        self._revert_error = revert_error

    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None:
        self.created.append((domain_name, name, include_memory))
        if self._create_error is not None:
            raise self._create_error

    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
        self.reverted.append((domain_name, name, start_paused))
        if self._revert_error is not None:
            raise self._revert_error

    def delete(self, domain_name: str, name: str) -> None:
        self.deleted.append((domain_name, name))

    def delete_all(self, domain_name: str) -> None:
        self.deleted_all.append(domain_name)


async def _seed_system(
    pool: AsyncConnectionPool,
    state: SystemState,
    *,
    provisioning_profile: dict[str, object] | None = None,
) -> UUID:
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=state,
                provisioning_profile=provisioning_profile or {},
                domain_name="kdive-x",
            ),
        )
    return system.id


async def _seed_snapshot(
    pool: AsyncConnectionPool, system_id: UUID, name: str, state: SnapshotState
) -> UUID:
    async with pool.connection() as conn:
        row = await SNAPSHOTS.insert(
            conn,
            Snapshot(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                system_id=system_id,
                name=name,
                include_memory=True,
                state=state,
            ),
        )
    return row.id


def _job(kind: JobKind, system_id: UUID, payload: dict[str, object]) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=kind,
        payload={"system_id": str(system_id), **payload},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": None, "project": "proj"},
        dedup_key=f"{system_id}:{kind.value}:x",
    )


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


async def _sys_state(conn: AsyncConnection, sid: UUID) -> SystemState:
    row = await SYSTEMS.get(conn, sid)
    assert row is not None
    return row.state


async def _snap_state(conn: AsyncConnection, snap_id: UUID) -> SnapshotState:
    row = await SNAPSHOTS.get(conn, snap_id)
    assert row is not None
    return row.state


def test_snapshot_success_drives_row_available_and_leaves_system_ready(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            snap_id = await _seed_snapshot(pool, sid, "before-bug", SnapshotState.CREATING)
            snapshotter = _FakeSnapshotter()
            resolver = provider_resolver(snapshotter=snapshotter)
            async with pool.connection() as conn:
                await snapshot_handler(
                    conn,
                    _job(
                        JobKind.SNAPSHOT,
                        sid,
                        {
                            "snapshot_id": str(snap_id),
                            "name": "before-bug",
                            "include_memory": True,
                        },
                    ),
                    resolver=resolver,
                )
                assert await _snap_state(conn, snap_id) is SnapshotState.AVAILABLE
                assert await _sys_state(conn, sid) is SystemState.READY
            assert snapshotter.created == [("kdive-x", "before-bug", True)]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_provider_error_marks_row_failed(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            snap_id = await _seed_snapshot(pool, sid, "cp", SnapshotState.CREATING)
            err = CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
            resolver = provider_resolver(snapshotter=_FakeSnapshotter(create_error=err))
            async with pool.connection() as conn:
                raised = False
                try:
                    await snapshot_handler(
                        conn,
                        _job(
                            JobKind.SNAPSHOT,
                            sid,
                            {"snapshot_id": str(snap_id), "name": "cp", "include_memory": True},
                        ),
                        resolver=resolver,
                    )
                except CategorizedError as exc:
                    raised = True
                    assert exc.terminal is True  # a failed capture dead-letters, does not retry
                assert raised
                assert await _snap_state(conn, snap_id) is SnapshotState.FAILED
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_retry_after_available_is_idempotent(migrated_url: str) -> None:
    # A completion-window worker crash re-delivers a SNAPSHOT job whose row already reached
    # `available`; the re-run must be a no-op success, not an available->available IllegalTransition
    # that dead-letters a snapshot that actually succeeded.
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            snap_id = await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            resolver = provider_resolver(snapshotter=_FakeSnapshotter())
            async with pool.connection() as conn:
                result = await snapshot_handler(
                    conn,
                    _job(
                        JobKind.SNAPSHOT,
                        sid,
                        {"snapshot_id": str(snap_id), "name": "cp", "include_memory": True},
                    ),
                    resolver=resolver,
                )
                assert result == str(snap_id)
                assert await _snap_state(conn, snap_id) is SnapshotState.AVAILABLE
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_snapshot_on_non_ready_system_fails_row_without_capturing(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.REPROVISIONING)
            snap_id = await _seed_snapshot(pool, sid, "cp", SnapshotState.CREATING)
            snapshotter = _FakeSnapshotter()
            resolver = provider_resolver(snapshotter=snapshotter)
            async with pool.connection() as conn:
                await snapshot_handler(
                    conn,
                    _job(
                        JobKind.SNAPSHOT,
                        sid,
                        {"snapshot_id": str(snap_id), "name": "cp", "include_memory": True},
                    ),
                    resolver=resolver,
                )
                assert await _snap_state(conn, snap_id) is SnapshotState.FAILED
            assert snapshotter.created == []
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_running_returns_system_to_ready(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.RESTORING)
            snapshotter = _FakeSnapshotter()
            resolver = provider_resolver(snapshotter=snapshotter)
            async with pool.connection() as conn:
                await restore_handler(
                    conn,
                    _job(JobKind.RESTORE, sid, {"name": "cp", "start_paused": False}),
                    resolver=resolver,
                )
                assert await _sys_state(conn, sid) is SystemState.READY
            assert snapshotter.reverted == [("kdive-x", "cp", False)]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_paused_lands_system_in_paused(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.RESTORING)
            resolver = provider_resolver(snapshotter=_FakeSnapshotter())
            async with pool.connection() as conn:
                await restore_handler(
                    conn,
                    _job(JobKind.RESTORE, sid, {"name": "cp", "start_paused": True}),
                    resolver=resolver,
                )
                assert await _sys_state(conn, sid) is SystemState.PAUSED
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_restore_provider_error_fails_the_system(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.RESTORING)
            err = CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
            resolver = provider_resolver(snapshotter=_FakeSnapshotter(revert_error=err))
            async with pool.connection() as conn:
                raised = False
                try:
                    await restore_handler(
                        conn,
                        _job(JobKind.RESTORE, sid, {"name": "cp", "start_paused": False}),
                        resolver=resolver,
                    )
                except CategorizedError:
                    raised = True
                assert raised
                assert await _sys_state(conn, sid) is SystemState.FAILED
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_delete_removes_libvirt_snapshot_and_ledger_row(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "cp", SnapshotState.AVAILABLE)
            snapshotter = _FakeSnapshotter()
            resolver = provider_resolver(snapshotter=snapshotter)
            async with pool.connection() as conn:
                await snapshot_delete_handler(
                    conn,
                    _job(JobKind.DELETE_SNAPSHOT, sid, {"name": "cp"}),
                    resolver=resolver,
                )
                assert await snapshot_by_name(conn, sid, "cp") is None
            assert snapshotter.deleted == [("kdive-x", "cp")]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_teardown_deletes_all_snapshots_and_ledger_rows(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(pool, SystemState.READY)
            await _seed_snapshot(pool, sid, "available-cp", SnapshotState.AVAILABLE)
            await _seed_snapshot(pool, sid, "creating-orphan", SnapshotState.CREATING)
            snapshotter = _FakeSnapshotter()
            resolver = provider_resolver(provisioner=FakeProvisioning(), snapshotter=snapshotter)
            async with pool.connection() as conn:
                await teardown_handler(
                    conn,
                    _job(JobKind.TEARDOWN, sid, {}),
                    resolver=resolver,
                )
                assert await _sys_state(conn, sid) is SystemState.TORN_DOWN
                assert await snapshots_for_system(conn, sid) == []
            assert snapshotter.deleted_all == ["kdive-x"]
        finally:
            await pool.close()

    asyncio.run(scenario())


def test_reprovision_deletes_snapshot_ledger_rows(migrated_url: str) -> None:
    async def scenario() -> None:
        pool = _pool(migrated_url)
        await pool.open()
        try:
            sid = await _seed_system(
                pool, SystemState.REPROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            await _seed_snapshot(pool, sid, "stale-cp", SnapshotState.AVAILABLE)
            fingerprint = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))
            resolver = provider_resolver(provisioner=FakeProvisioning())
            job = _job(JobKind.REPROVISION, sid, {"profile_digest": fingerprint})
            async with pool.connection() as conn:
                await reprovision_handler(conn, job, resolver=resolver)
                assert await _sys_state(conn, sid) is SystemState.READY
                assert await snapshot_by_name(conn, sid, "stale-cp") is None
        finally:
            await pool.close()

    asyncio.run(scenario())
