"""The reconciler ``console_rotate`` dispatch sweep for live local Systems (#892).

Each pass enqueues one ``console_rotate`` worker job per booted local-libvirt System that has no
pending/running rotation job. Liveness is keyed on the System, not on a Run: a ``ready`` System
whose most recent Run already ``succeeded`` must still be rotated (the #892 repro), because the
in-guest workload keeps emitting console after the Run reaches a terminal state. Rotation stops
only when the System is torn down. A remote-libvirt System is never a candidate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, RunState, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, System
from kdive.reconciler.repairs import console_rotation as console_rotation_repairs
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_running_job, seed_system

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_SWEEP = console_rotation_repairs.sweep_console_rotation


async def _seed_remote_system(
    conn: psycopg.AsyncConnection, *, system_state: SystemState = SystemState.READY
) -> UUID:
    """Insert a remote-libvirt resource -> active allocation -> System; return the system id."""
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.REMOTE_LIBVIRT,
            pool="p",
            cost_class="remote",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu+ssh://host/system",
        ),
    )
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=AllocationState.ACTIVE,
        ),
    )
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            allocation_id=allocation.id,
            state=system_state,
            provisioning_profile={"k": "v"},
        ),
    )
    return system.id


async def _rotation_payloads(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Every enqueued ``console_rotate`` job payload, oldest first."""
    cur = await conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'console_rotate' ORDER BY created_at, id"
    )
    return [row[0] for row in await cur.fetchall()]


def test_two_live_local_systems_enqueue_two_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid_a = await seed_system(seed, system_state=SystemState.READY)
            sid_b = await seed_system(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 2
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        assert {p["system_id"] for p in payloads} == {str(sid_a), str(sid_b)}
        for payload in payloads:
            # Not co-located with the console file here, so the per-boot identity degrades to the
            # empty reset-forcing string (never a placeholder / absent key).
            assert payload["boot_id"] == ""

    asyncio.run(_run())


def test_boot_id_stamps_real_console_stat_when_colocated(
    migrated_url: str, tmp_path: Any, monkeypatch: Any
) -> None:
    # When the console file is reachable, the payload carries the file's real dev:ino:mtime
    # identity keyed on THIS system's path — not an empty string, and not some other file's.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid = await seed_system(seed, system_state=SystemState.READY)

        def _path_for(system_id: UUID) -> Any:
            return tmp_path / f"{system_id}.log"

        monkeypatch.setattr(console_rotation_repairs, "console_log_path", _path_for)
        console_file = _path_for(sid)
        console_file.write_bytes(b"console output")
        st = console_file.stat()
        expected = f"{st.st_dev}:{st.st_ino}:{int(st.st_mtime)}"

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 1
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        assert [p["boot_id"] for p in payloads] == [expected]
        assert expected != ""  # a real stat produced a non-empty identity

    asyncio.run(_run())


def test_skipped_in_flight_system_does_not_halt_remaining(migrated_url: str) -> None:
    # A System with an in-flight rotation is skipped, but the sweep must CONTINUE to the other
    # live Systems (not break out of the loop). The skipped System is seeded first so a `break`
    # regression would drop the two free Systems that follow it.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            skipped = await seed_system(seed, system_state=SystemState.READY)
            await seed_running_job(
                seed,
                f"console_rotate:{skipped}:preexisting",
                kind="console_rotate",
                payload={"system_id": str(skipped)},
                lease_seconds=300,
                attempt=0,
                max_attempts=3,
            )
            free_a = await seed_system(seed, system_state=SystemState.READY)
            free_b = await seed_system(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 2  # both free Systems enqueued despite the earlier skip
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        enqueued = {p["system_id"] for p in payloads if p.get("boot_id") is not None}
        assert {str(free_a), str(free_b)} <= enqueued

    asyncio.run(_run())


def test_in_flight_rotation_is_not_re_enqueued(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid = await seed_system(seed, system_state=SystemState.READY)
            await seed_running_job(
                seed,
                f"console_rotate:{sid}:preexisting",
                kind="console_rotate",
                payload={"system_id": str(sid)},
                lease_seconds=300,
                attempt=0,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 0  # the in-flight job dedups the System
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        assert len(payloads) == 1  # only the pre-seeded job

    asyncio.run(_run())


def test_ready_system_with_succeeded_run_still_enqueued(migrated_url: str) -> None:
    # The #892 case: the System is `ready` while its most recent Run already `succeeded`, yet the
    # in-guest workload keeps emitting console. Liveness keys on the System, never on the Run, so
    # a terminal Run must NOT stop rotation.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid = await seed_system(seed, system_state=SystemState.READY)
            await seed_run(seed, sid, run_state=RunState.SUCCEEDED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 1
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        assert [p["system_id"] for p in payloads] == [str(sid)]

    asyncio.run(_run())


def test_remote_libvirt_system_not_enqueued(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_remote_system(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _rotation_payloads(check) == []

    asyncio.run(_run())


def test_torn_down_system_not_enqueued(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _rotation_payloads(check) == []

    asyncio.run(_run())


def test_second_pass_does_not_duplicate(migrated_url: str) -> None:
    # The job the first pass enqueues is `queued` (in flight), so the second pass dedups it: the
    # sweep is idempotent under its own re-run even though each enqueue carries a unique dedup key.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, _SWEEP)
            second = await run_repair(pool, _SWEEP)
        assert first == 1
        assert second == 0
        async with await connect(migrated_url) as check:
            assert len(await _rotation_payloads(check)) == 1

    asyncio.run(_run())
