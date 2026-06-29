"""Tests for the remote per-Run console snapshotter (ADR-0235).

The snapshotter assembles a System's already-uploaded S3 console parts into an immutable
``console-<run>`` artifact, writing its row on the boot handler's connection. These tests mock the
S3 boundary (``object_store_from_env``) with the in-memory part store and exercise the row writes
against a migrated database.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.providers.remote_libvirt.console import snapshot as snapshot_mod
from kdive.providers.remote_libvirt.console.snapshot import RemoteLibvirtConsoleSnapshotter
from kdive.providers.remote_libvirt.console.wiring import RemoteConsolePartStore
from tests.providers.remote_libvirt.console.test_console_wiring import FakeObjectStore


def _seed_parts(store: FakeObjectStore, system_id: UUID, parts: list[bytes], conninfo: str) -> None:
    part_store = RemoteConsolePartStore(store, conninfo)
    for index, data in enumerate(parts):
        part_store.put_part(system_id, index, data)


async def _run_snapshot(migrated_url: str, system_id: UUID, run_id: UUID):
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        return await RemoteLibvirtConsoleSnapshotter().snapshot(conn, system_id, run_id)


async def _count_rows(migrated_url: str, system_id: UUID, object_key: str) -> int:
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM artifacts WHERE owner_id = %s AND object_key = %s",
            (system_id, object_key),
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def _run_mark(system_id: UUID) -> int:
    return await RemoteLibvirtConsoleSnapshotter().mark_boot_window(system_id)


async def _run_snapshot_sliced(migrated_url: str, system_id: UUID, run_id: UUID, start_index: int):
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        return await RemoteLibvirtConsoleSnapshotter().snapshot(
            conn, system_id, run_id, start_index
        )


def test_mark_boot_window_is_next_part_index(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id = uuid4()
    _seed_parts(store, system_id, [b"a", b"b"], migrated_url)  # parts 0, 1
    assert asyncio.run(_run_mark(system_id)) == 2


def test_mark_boot_window_zero_when_no_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    assert asyncio.run(_run_mark(uuid4())) == 0


def test_snapshot_slices_to_boot_window(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Prior boot wrote parts 0..1 (ending in a panic); this boot's window starts at the mark.
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"prior ", b"Kernel panic\n"], migrated_url)  # parts 0, 1

    mark = asyncio.run(_run_mark(system_id))  # == 2
    _seed_parts(
        store, system_id, [b"prior ", b"Kernel panic\n", b"this boot READY\n"], migrated_url
    )  # +part 2

    snap = asyncio.run(_run_snapshot_sliced(migrated_url, system_id, run_id, mark))

    assert snap is not None
    assert snap.data == b"this boot READY\n"  # no prior-boot panic in the window
    assert asyncio.run(_count_rows(migrated_url, system_id, snap.object_key)) == 1


def test_snapshot_empty_window_returns_none(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Healthy boot whose bytes never rotated into a part: the window is empty -> no artifact.
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"prior boot"], migrated_url)  # part 0
    mark = asyncio.run(_run_mark(system_id))  # == 1, nothing at/after it

    snap = asyncio.run(_run_snapshot_sliced(migrated_url, system_id, run_id, mark))

    assert snap is None
    key = f"remote-libvirt/systems/{system_id}/console-{run_id}"
    assert asyncio.run(_count_rows(migrated_url, system_id, key)) == 0


def test_snapshot_assembles_parts_into_per_run_artifact(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"boot ...\n", b"Kernel panic\n"], migrated_url)

    snap = asyncio.run(_run_snapshot(migrated_url, system_id, run_id))

    assert snap is not None
    assert snap.data == b"boot ...\nKernel panic\n"
    key = f"remote-libvirt/systems/{system_id}/console-{run_id}"
    assert snap.object_key == key
    assert store.objects[key] == b"boot ...\nKernel panic\n"
    assert asyncio.run(_count_rows(migrated_url, system_id, key)) == 1


def test_snapshot_keys_distinct_runs_to_distinct_rows(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_a, run_b = uuid4(), uuid4(), uuid4()
    _seed_parts(store, system_id, [b"first boot crash"], migrated_url)
    snap_a = asyncio.run(_run_snapshot(migrated_url, system_id, run_a))

    # A later boot of the same System rotates more parts; the snapshot keys to its own Run.
    _seed_parts(store, system_id, [b"first boot crash", b" + second boot"], migrated_url)
    snap_b = asyncio.run(_run_snapshot(migrated_url, system_id, run_b))

    assert snap_a is not None and snap_b is not None
    assert snap_a.id != snap_b.id
    assert snap_a.object_key != snap_b.object_key
    assert asyncio.run(_count_rows(migrated_url, system_id, snap_a.object_key)) == 1
    assert asyncio.run(_count_rows(migrated_url, system_id, snap_b.object_key)) == 1


def test_snapshot_resnapshot_same_run_refreshes_in_place(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"crash"], migrated_url)
    snap_one = asyncio.run(_run_snapshot(migrated_url, system_id, run_id))
    snap_two = asyncio.run(_run_snapshot(migrated_url, system_id, run_id))

    assert snap_one is not None and snap_two is not None
    assert snap_one.id == snap_two.id  # same per-Run key → row refreshed, not duplicated
    assert asyncio.run(_count_rows(migrated_url, system_id, snap_one.object_key)) == 1


def test_snapshot_returns_none_when_no_parts(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()

    snap = asyncio.run(_run_snapshot(migrated_url, system_id, run_id))

    assert snap is None
    key = f"remote-libvirt/systems/{system_id}/console-{run_id}"
    assert key not in store.objects
    assert asyncio.run(_count_rows(migrated_url, system_id, key)) == 0
