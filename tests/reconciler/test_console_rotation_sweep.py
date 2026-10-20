"""The reconciler ``console_rotate`` dispatch sweep for live local Systems (#892).

Each pass enqueues one ``console_rotate`` worker job per booted local-libvirt System that has no
pending/running rotation job. Liveness is keyed on the System, not on a Run: a ``ready`` System
whose most recent Run already ``succeeded`` must still be rotated (the #892 repro), because the
in-guest workload keeps emitting console after the Run reaches a terminal state. Rotation stops
only when the System is torn down. A remote-libvirt System is never a candidate.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.console.rotation import RotationState, rotate
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, RunState, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, System
from kdive.providers.local_libvirt.lifecycle import storage as storage_module
from kdive.reconciler.repairs import console_rotation as console_rotation_repairs
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_running_job, seed_system

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_SWEEP = console_rotation_repairs.sweep_console_rotation
_INITIAL_STATE = RotationState(
    plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None
)


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


def test_boot_id_stamps_real_console_identity_when_colocated(
    migrated_url: str, tmp_path: Any, monkeypatch: Any
) -> None:
    # When the console file is reachable, the payload carries the file's dev:ino plus the
    # per-start nonce the prepare seam recorded, keyed on THIS system's path (#3254).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid = await seed_system(seed, system_state=SystemState.READY)

        def _path_for(system_id: UUID) -> Any:
            return tmp_path / f"{system_id}.log"

        monkeypatch.setattr(console_rotation_repairs, "console_log_path", _path_for)
        console_file = _path_for(sid)
        storage_module._prepare_console_log(console_file)
        console_file.write_bytes(b"console output")
        st = console_file.stat()
        nonce = (tmp_path / f"{sid}.boot").read_text()
        expected = f"{st.st_dev}:{st.st_ino}:{nonce}"

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _SWEEP)
        assert count == 1
        async with await connect(migrated_url) as check:
            payloads = await _rotation_payloads(check)
        assert [p["boot_id"] for p in payloads] == [expected]
        assert len(nonce) == 32

    asyncio.run(_run())


def _identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Callable[[], str]]:
    sid = uuid4()
    log = tmp_path / f"{sid}.log"
    monkeypatch.setattr(console_rotation_repairs, "console_log_path", lambda _sid: log)
    return log, lambda: console_rotation_repairs._boot_id(sid)


def _redact(data: bytes) -> bytes:
    return data


def _boot_bytes(tag: bytes, size: int) -> bytes:
    return (tag * (size // len(tag) + 1))[:size]


def test_console_growth_keeps_the_boot_identity_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #3254: appends (a crash's panic output among them) advance mtime but are the same boot.
    # The old dev:ino:mtime identity re-cut the whole log into a new generation here.
    log, boot_id = _identity(tmp_path, monkeypatch)
    storage_module._prepare_console_log(log)
    log.write_bytes(_boot_bytes(b"boot-A ", 100_000))
    first = boot_id()
    state = rotate(_INITIAL_STATE, log.read_bytes(), first, _redact).next_state

    with log.open("ab") as stream:
        stream.write(_boot_bytes(b"Kernel panic - not syncing ", 80_000))
    later = log.stat().st_mtime + 120
    os.utime(log, (later, later))
    second = boot_id()
    result = rotate(state, log.read_bytes(), second, _redact)

    assert second == first
    assert {part.gen for part in result.parts} == {0}
    assert result.next_state.plaintext_offset == 180_000


def test_power_cycle_regrowing_past_the_offset_starts_a_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0258/ADR-0273: a power-cycle truncates the same inode; a new boot that regrows past
    # the prior cursor before the next rotation must still read as a new boot.
    log, boot_id = _identity(tmp_path, monkeypatch)
    storage_module._prepare_console_log(log)
    log.write_bytes(_boot_bytes(b"boot-A ", 70_000))
    first = boot_id()
    state = rotate(_INITIAL_STATE, log.read_bytes(), first, _redact).next_state

    storage_module._prepare_console_log(log)
    with log.open("ab") as stream:
        stream.write(_boot_bytes(b"boot-B ", 150_000))
    second = boot_id()
    result = rotate(state, log.read_bytes(), second, _redact)

    assert second != first
    assert {part.gen for part in result.parts} == {1}
    assert result.parts[0].index == 0
    assert result.parts[0].redacted.startswith(b"boot-B ")


def test_power_cycle_truncate_then_shrink_still_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rotation that sees the truncated log before the new nonce lands resets on the shrink
    # check; the nonce then resets once more, and no prior-boot bytes reach the new boot.
    log, boot_id = _identity(tmp_path, monkeypatch)
    storage_module._prepare_console_log(log)
    log.write_bytes(_boot_bytes(b"boot-A ", 70_000))
    state = rotate(_INITIAL_STATE, log.read_bytes(), boot_id(), _redact).next_state
    stale = boot_id()

    log.write_bytes(b"")
    shrunk = rotate(state, log.read_bytes(), stale, _redact)
    storage_module._prepare_console_log(log)
    log.write_bytes(_boot_bytes(b"boot-B ", 70_000))
    regrown = rotate(shrunk.next_state, log.read_bytes(), boot_id(), _redact)

    assert shrunk.parts == [] and shrunk.next_state.boot_gen == 1
    assert {part.gen for part in regrown.parts} == {2}
    assert all(b"boot-A" not in part.redacted for part in regrown.parts)


def test_boot_id_degrades_to_empty_when_the_log_cannot_be_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reconciler not co-located with the worker cannot see the log at all.
    _log, boot_id = _identity(tmp_path, monkeypatch)
    assert boot_id() == ""


@pytest.mark.parametrize("marker_kind", ["missing", "symlink", "fifo", "oversized", "malformed"])
def test_untrusted_boot_marker_contributes_an_empty_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    marker_kind: str,
) -> None:
    # The marker is read across a trust boundary into a persisted payload: anything but a
    # worker-owned regular file holding exactly one nonce yields a stable empty nonce, and a
    # FIFO must not block the serial sweep.
    log, boot_id = _identity(tmp_path, monkeypatch)
    log.write_bytes(b"console")
    marker = log.with_suffix(".boot")
    if marker_kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text("a" * 32)
        marker.symlink_to(target)
    elif marker_kind == "fifo":
        # A FIFO whose writer offers a well-formed nonce: only a regular file is trusted.
        os.mkfifo(marker)
        writer = os.open(marker, os.O_RDWR | os.O_NONBLOCK)
        os.write(writer, b"a" * 32)
        request.addfinalizer(lambda: os.close(writer))
    elif marker_kind == "oversized":
        marker.write_text("a" * 100)
    elif marker_kind == "malformed":
        marker.write_text("not-a-hex-nonce-but-32-chars-xx!")
    st = log.stat()

    assert boot_id() == f"{st.st_dev}:{st.st_ino}:"


def test_foreign_owned_boot_marker_contributes_an_empty_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, boot_id = _identity(tmp_path, monkeypatch)
    storage_module._prepare_console_log(log)
    st = log.stat()
    real_fstat = os.fstat

    def _foreign_fstat(fd: int) -> os.stat_result:
        found = real_fstat(fd)
        fields = list(found)
        fields[4] = found.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(console_rotation_repairs.os, "fstat", _foreign_fstat)

    assert boot_id() == f"{st.st_dev}:{st.st_ino}:"


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
