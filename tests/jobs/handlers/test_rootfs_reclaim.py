"""The ``reclaim_investigation_rootfs`` worker handler (ADR-0442, #1522).

The reclaim's filesystem half moved from the reconciler to the worker because a root-owned staging
tree is not writable by an unprivileged reconciler. These cover the regression that motivated the
move, the ADR-0441 §6 liveness gate the handler still enforces, the flipped file -> object -> row
order, and the drain/marker bookkeeping that replaced the sweep's per-pass ``drained`` flag.

Async-DB tests follow the in-repo pattern: a sync ``def test_(migrated_url)`` with an inner
``async def _run()`` driven by ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.artifacts.content_address import rootfs_object_token
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.artifacts.rootfs_reclaim import reclaim_investigation_rootfs_handler
from kdive.providers.local_libvirt.lifecycle.storage import overlay_name
from tests.reconciler.conftest import connect

_FROZEN = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def _checksum(seed: bytes) -> str:
    return base64.b64encode(hashlib.sha256(seed).digest()).decode("ascii")


_CHECKSUM = _checksum(b"rootfs")
_TOKEN = rootfs_object_token(_CHECKSUM)
_CHECKSUM_Y = _checksum(b"rootfs-y")


class _RecordingStore:
    """An object store that records deletes, optionally failing on a chosen key."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.deleted: list[str] = []
        self._fail_on = fail_on

    def delete(self, key: str) -> None:
        if key == self._fail_on:
            raise RuntimeError(f"store delete of {key} failed")
        self.deleted.append(key)


def _object_key(inv: UUID) -> str:
    return f"local/investigations/{inv}/rootfs-{_TOKEN}"


def _upload_profile(checksum: str = _CHECKSUM) -> str:
    return json.dumps(
        {"provider": {"local-libvirt": {"rootfs": {"kind": "upload", "checksum_sha256": checksum}}}}
    )


def _catalog_profile() -> str:
    return json.dumps(
        {"provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "rhel"}}}}
    )


async def _seed_investigation(conn: psycopg.AsyncConnection, *, state: str, closed: bool) -> UUID:
    inv_id = uuid4()
    marker = "now()" if closed else "NULL"
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state, "
        f"rootfs_cleanup_pending_at) VALUES (%s, 'p', 'proj', 't', %s, {marker})",
        (inv_id, state),
    )
    return inv_id


async def _seed_system(conn: psycopg.AsyncConnection, inv: UUID, state: str, profile: str) -> UUID:
    resource_id, alloc_id, system_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (alloc_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, investigation_id, state, provisioning_profile, "
        "principal, project) VALUES (%s, %s, %s, %s, %s::jsonb, 'p', 'proj')",
        (system_id, alloc_id, inv, state, profile),
    )
    return system_id


async def _seed_rootfs_row(
    conn: psycopg.AsyncConnection, inv: UUID, key: str | None = None
) -> UUID:
    artifact_id = uuid4()
    await conn.execute(
        "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, 'investigations', %s, %s, 'e', 'redacted', 'rootfs')",
        (artifact_id, inv, key or _object_key(inv)),
    )
    return artifact_id


def _stage(uploads_dir: Path, inv: UUID) -> Path:
    staged = uploads_dir / str(inv) / f"{_TOKEN}.qcow2"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"base")
    return staged


def _job(inv: UUID, artifact_ids: list[UUID]) -> Job:
    return Job(
        id=uuid4(),
        created_at=_FROZEN,
        updated_at=_FROZEN,
        kind=JobKind.RECLAIM_INVESTIGATION_ROOTFS,
        payload={
            "investigation_id": str(inv),
            "artifact_ids": [str(a) for a in artifact_ids],
        },
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "reconciler", "agent_session": None, "project": "proj"},
        dedup_key=f"rootfs-reclaim:{inv}",
    )


async def _row_exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


async def _marker(conn: psycopg.AsyncConnection, inv: UUID) -> object:
    cur = await conn.execute(
        "SELECT rootfs_cleanup_pending_at FROM investigations WHERE id = %s", (inv,)
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _run_handler(
    migrated_url: str,
    inv: UUID,
    artifact_ids: list[UUID],
    store: _RecordingStore,
    rootfs_dir: Path,
    uploads: Path,
) -> str | None:
    conn = await connect(migrated_url)
    try:
        return await reclaim_investigation_rootfs_handler(
            conn,
            _job(inv, artifact_ids),
            artifact_store=store,
            rootfs_dir=str(rootfs_dir),
            uploads_dir=str(uploads),
        )
    finally:
        await conn.close()


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    rootfs_dir = tmp_path / "rootfs"
    rootfs_dir.mkdir(exist_ok=True)
    uploads = tmp_path / "rootfs-uploads"
    uploads.mkdir(exist_ok=True)
    return rootfs_dir, uploads


def test_reclaims_base_object_row_and_marker(migrated_url: str, tmp_path: Path) -> None:
    # The path #1522 had no working version of: a closed investigation past grace whose only
    # referencer is torn_down drains completely — staged base, object, row, marker, staging dir.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
            await _seed_system(seed, inv, "torn_down", _upload_profile())
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        store = _RecordingStore()

        result = await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)

        assert result == "1"
        assert not staged.exists()
        assert store.deleted == [_object_key(inv)]
        assert not staged.parent.exists()  # empty staging dir removed once drained
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


@pytest.mark.skipif(
    os.geteuid() == 0, reason="a root process ignores directory write bits, so EPERM cannot be set"
)
def test_unlink_permission_fault_never_deletes_the_object_or_row(
    migrated_url: str, tmp_path: Path
) -> None:
    # #1522's regression, with the real EPERM the live proof hit (dropping the staging dir's write
    # bit blocks unlink even for the file's owner) rather than root. The harm was that the object
    # was ALREADY gone by the time the unlink failed, stranding a bootable SENSITIVE base with no
    # recoverable copy. Under ADR-0442's file -> object -> row order that is structurally
    # impossible: nothing is deleted, and the job fails loudly instead of warning every 30 s.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
            await _seed_system(seed, inv, "torn_down", _upload_profile())
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        staged.parent.chmod(0o555)  # read+exec, no write: unlink inside raises EPERM
        store = _RecordingStore()
        try:
            with pytest.raises(CategorizedError):
                await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)
            assert store.deleted == []  # the object was NOT deleted ahead of the failed unlink
            assert staged.exists()
            check = await connect(migrated_url)
            try:
                assert await _row_exists(check, artifact_id)
                assert await _marker(check, inv) is not None  # still on the worklist
            finally:
                await check.close()
        finally:
            staged.parent.chmod(0o755)  # let tmp_path teardown clean up

        # Once the permission wall is gone the same job drains with no manual repair.
        ok_store = _RecordingStore()
        assert await _run_handler(migrated_url, inv, [artifact_id], ok_store, rootfs_dir, uploads)
        assert ok_store.deleted == [_object_key(inv)]
        assert not staged.exists()

    asyncio.run(_run())


def test_unlinks_the_staged_base_before_deleting_the_object(
    migrated_url: str, tmp_path: Path
) -> None:
    # ADR-0442 §4: file -> object -> row. A store fault must therefore find the local SENSITIVE
    # base already gone (the copy worth losing first) and leave the row for the retry.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        store = _RecordingStore(fail_on=_object_key(inv))

        with pytest.raises(CategorizedError):
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)

        assert not staged.exists()  # the local base went first
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)  # row kept: the object survives
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

        # The retry converges: the unlink is now ENOENT (success) and the delete succeeds.
        ok_store = _RecordingStore()
        assert await _run_handler(migrated_url, inv, [artifact_id], ok_store, rootfs_dir, uploads)
        assert ok_store.deleted == [_object_key(inv)]
        check2 = await connect(migrated_url)
        try:
            assert not await _row_exists(check2, artifact_id)
            assert await _marker(check2, inv) is None
        finally:
            await check2.close()

    asyncio.run(_run())


def test_live_overlay_pins_the_base_and_the_job_succeeds(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0441 §6 condition (a) survives the move, and a pin is NOT a job failure: pinning is the
    # steady state for the whole grace window, so it must not dead-letter the reclaim slot.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
            sys_id = await _seed_system(seed, inv, "ready", _upload_profile())
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        overlay = rootfs_dir / overlay_name(str(sys_id))
        overlay.write_bytes(b"overlay")
        partial = uploads / str(inv) / f"{_TOKEN}.{uuid4()}.partial"
        partial.write_bytes(b"in-flight download")
        store = _RecordingStore()

        assert (
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == "0"
        )
        assert store.deleted == []
        assert staged.exists()
        assert partial.exists()  # an in-flight fetch is not clobbered while a row remains
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

        overlay.unlink()  # teardown reclaimed the overlay
        assert (
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == "1"
        )
        assert not staged.exists()
        assert not partial.exists()  # now a crash orphan; swept with the drained dir

    asyncio.run(_run())


def test_pre_overlay_state_pins_without_an_overlay_file(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0441 §6 condition (b): a re-materializing referencer pins even with no overlay on disk.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
            await _seed_system(seed, inv, "reprovisioning", _upload_profile())
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        store = _RecordingStore()

        assert (
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == "0"
        )
        assert staged.exists()
        assert store.deleted == []

    asyncio.run(_run())


def test_unrelated_referencers_do_not_pin(migrated_url: str, tmp_path: Path) -> None:
    # A live System referencing a DIFFERENT checksum or a catalog rootfs is not a referencer.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
            other = await _seed_system(seed, inv, "ready", _upload_profile(_CHECKSUM_Y))
            cat = await _seed_system(seed, inv, "ready", _catalog_profile())
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        _stage(uploads, inv)
        (rootfs_dir / overlay_name(str(other))).write_bytes(b"overlay")
        (rootfs_dir / overlay_name(str(cat))).write_bytes(b"overlay")
        store = _RecordingStore()

        assert (
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == "1"
        )

    asyncio.run(_run())


def test_already_deleted_artifact_id_is_a_no_op(migrated_url: str, tmp_path: Path) -> None:
    # A stale due-set (the row drained by a prior attempt) must not fail the job.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        store = _RecordingStore()

        assert await _run_handler(migrated_url, inv, [uuid4()], store, rootfs_dir, uploads) == "0"
        assert store.deleted == []
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is None  # no rows remain -> marker cleared
        finally:
            await check.close()

    asyncio.run(_run())


def test_an_empty_worklist_still_sweeps_the_staging_dir_and_clears_the_marker(
    migrated_url: str, tmp_path: Path
) -> None:
    # The sweep enqueues a job even when an investigation past grace has no rootfs rows, because
    # this is the only path that reaps a crash-orphaned SENSITIVE `*.partial` no row owns and clears
    # the marker. Short-circuiting it in the reconciler would strand the orphan.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        inv_dir = uploads / str(inv)
        inv_dir.mkdir(parents=True)
        orphan = inv_dir / f"{_TOKEN}.{uuid4()}.partial"
        orphan.write_bytes(b"crash-orphaned download")
        store = _RecordingStore()

        assert await _run_handler(migrated_url, inv, [], store, rootfs_dir, uploads) == "0"

        assert not orphan.exists()
        assert not inv_dir.exists()
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


def test_marker_survives_a_partially_drained_investigation(
    migrated_url: str, tmp_path: Path
) -> None:
    # The drain rule is a state query, not a per-pass flag: one drained checksum plus one pinned
    # checksum keeps the marker AND the staging dir.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            drainable = await _seed_rootfs_row(seed, inv)
            token_y = rootfs_object_token(_CHECKSUM_Y)
            pinned = await _seed_rootfs_row(
                seed, inv, key=f"local/investigations/{inv}/rootfs-{token_y}"
            )
            sys_id = await _seed_system(seed, inv, "ready", _upload_profile(_CHECKSUM_Y))
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        _stage(uploads, inv)
        (rootfs_dir / overlay_name(str(sys_id))).write_bytes(b"overlay")
        store = _RecordingStore()

        assert (
            await _run_handler(migrated_url, inv, [drainable, pinned], store, rootfs_dir, uploads)
            == "1"
        )
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, drainable)
            assert await _row_exists(check, pinned)
            assert await _marker(check, inv) is not None  # marker held by the pinned checksum
        finally:
            await check.close()
        assert (uploads / str(inv)).exists()  # staging dir kept while a row remains

    asyncio.run(_run())


def test_one_fault_does_not_starve_the_other_checksums(migrated_url: str, tmp_path: Path) -> None:
    # A real fault on one checksum still lets the rest drain, then fails the job so the stuck
    # reclaim is durable and visible instead of a repeating WARN line.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            faulting = await _seed_rootfs_row(seed, inv)
            token_y = rootfs_object_token(_CHECKSUM_Y)
            ok = await _seed_rootfs_row(
                seed, inv, key=f"local/investigations/{inv}/rootfs-{token_y}"
            )
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        store = _RecordingStore(fail_on=_object_key(inv))

        with pytest.raises(CategorizedError):
            await _run_handler(migrated_url, inv, [faulting, ok], store, rootfs_dir, uploads)

        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, faulting)  # deferred before its row delete
            assert not await _row_exists(check, ok)  # the healthy checksum still drained
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

    asyncio.run(_run())


def test_investigation_lock_serializes_a_concurrent_bind(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0442 §3: the handler holds the INVESTIGATION lock that System bind holds transaction-
    # scoped until its row commits, so a bind racing the gate cannot slip a live referencer in
    # between the gate read and the unlink. Without the lock the handler would run to completion
    # while the bind is still uncommitted and unlink a base the new System is about to back onto.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        store = _RecordingStore()

        binder = await connect(migrated_url)
        handler: asyncio.Task[str | None] | None = None
        try:
            async with (
                binder.transaction(),
                advisory_xact_lock(binder, LockScope.INVESTIGATION, inv),
            ):
                # The bind's System row is inserted but NOT yet committed, and its lock is held.
                await _seed_system(binder, inv, "provisioning", _upload_profile())
                handler = asyncio.create_task(
                    _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)
                )
                await asyncio.sleep(0.3)
                assert not handler.done()  # blocked on the lock the binder holds
                assert staged.exists()
        finally:
            await binder.close()

        assert await asyncio.wait_for(handler, timeout=10) == "0"
        # The handler resumed only after the bind committed, so it saw the pre-overlay referencer.
        assert staged.exists()
        assert store.deleted == []
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)
        finally:
            await check.close()

    asyncio.run(_run())
