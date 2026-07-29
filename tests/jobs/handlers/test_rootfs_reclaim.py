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
import errno
import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.artifacts.content_address import rootfs_object_token
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.artifacts import rootfs_reclaim
from kdive.jobs.handlers.artifacts.rootfs_reclaim import (
    ArtifactObjectDeleter,
    reclaim_investigation_rootfs_handler,
)
from kdive.providers.local_libvirt.lifecycle.storage import overlay_name
from kdive.providers.shared.runtime_paths import staged_rootfs_marker_path
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
    """A staged base as a completed publish leaves it: the qcow2 plus its ADR-0451 marker.

    Both, because a reclaim that removed only the base would leave the marker keeping the staging
    directory non-empty forever — the leak ADR-0443 deferred the marker for. Every test here that
    asserts the directory drains is therefore also asserting the marker was collected.
    """
    staged = uploads_dir / str(inv) / f"{_TOKEN}.qcow2"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"base")
    staged_rootfs_marker_path(staged).touch()
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
    store: ArtifactObjectDeleter,
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


def test_a_marker_unlink_fault_defers_the_whole_checksum(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0451 section 5 / ADR-0442 section 4. The completion marker is unlinked in the SAME
    # OSError-propagating region as the base, so a fault on it stops the checksum before the object
    # or the row is deleted. A marker unlink wrapped in its own `suppress(OSError)` -- the reflex
    # for a "best effort" sidecar -- would let the reclaim march on and delete the last recoverable
    # copy
    # of a SENSITIVE base while the local one is still sitting there, which is #1522 verbatim.
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
        marker = staged_rootfs_marker_path(staged)
        store = _RecordingStore()
        real_unlink = os.unlink

        def refusing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == marker:
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            real_unlink(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "unlink", refusing_unlink)
            with pytest.raises(CategorizedError):
                await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)

        assert store.deleted == []  # nothing dropped while the local base is still there
        assert staged.exists()  # and the marker went FIRST, so the base is untouched
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is not None  # still on the worklist
        finally:
            await check.close()

        # The retry converges once the fault clears, with no manual repair.
        ok_store = _RecordingStore()
        assert await _run_handler(migrated_url, inv, [artifact_id], ok_store, rootfs_dir, uploads)
        assert not staged.exists() and not marker.exists()

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


def test_the_first_fault_stops_the_loop_and_fails_the_job(
    migrated_url: str, tmp_path: Path
) -> None:
    # A refusing store is a store-wide condition and the delete budget is per-call, so pressing on
    # through the worklist would multiply that budget by its length while the worker slot and the
    # INVESTIGATION lock stay held. The loop stops at the first real fault; the untouched checksums
    # keep their rows and are re-attempted by the next sweep, and the job fails so it is visible.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            faulting = await _seed_rootfs_row(seed, inv)
            token_y = rootfs_object_token(_CHECKSUM_Y)
            untouched = await _seed_rootfs_row(
                seed, inv, key=f"local/investigations/{inv}/rootfs-{token_y}"
            )
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        store = _RecordingStore(fail_on=_object_key(inv))

        with pytest.raises(CategorizedError):
            await _run_handler(migrated_url, inv, [faulting, untouched], store, rootfs_dir, uploads)

        assert store.deleted == []  # the second checksum was never attempted
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, faulting)  # deferred before its row delete
            assert await _row_exists(check, untouched)
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


def test_a_hung_store_delete_is_bounded_and_defers_the_checksum(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The delete runs inside the transaction holding the INVESTIGATION lock, and the TTL backstop
    # reclaims live open/active investigations — so an unbounded store call would stall a bind, a
    # close, or a runs.create. The budget must fire, the checksum must defer with its row kept, and
    # the TimeoutError must surface as the handler's INFRASTRUCTURE_FAILURE rather than escaping.
    monkeypatch.setattr(rootfs_reclaim, "_STORE_DELETE_TIMEOUT_S", 0.2)
    released = threading.Event()
    inv = uuid4()

    class _HangingStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete(self, key: str) -> None:
            released.wait(timeout=30)
            self.deleted.append(key)

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="open", closed=False)
            artifact_id = await _seed_rootfs_row(seed, inv)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        staged = _stage(uploads, inv)
        store = _HangingStore()

        try:
            started = time.monotonic()
            with pytest.raises(CategorizedError) as excinfo:
                await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)
            assert time.monotonic() - started < 10  # bounded well inside the real 10 s budget
            assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        finally:
            # Release before the loop shuts its executor down, or asyncio.run waits out the thread.
            released.set()

        assert not staged.exists()  # the local base went first, as ordered
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)  # deferred before the row delete
        finally:
            await check.close()

    asyncio.run(_run())


def test_reclaim_reconverges_when_both_targets_are_already_absent(
    migrated_url: str, tmp_path: Path
) -> None:
    # The property the file->object->row order, the fault contract, and the "a worker that dies
    # mid-reclaim resumes" claim all rest on: an already-unlinked base (ENOENT) and an already-gone
    # object (a 404 the store no-ops) are SUCCESS, so a re-attempt after a partial run completes
    # the row delete instead of wedging.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            artifact_id = await _seed_rootfs_row(seed, inv)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)  # no staged base, no overlay
        store = _RecordingStore()

        assert await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == (
            "1"
        )
        assert store.deleted == [_object_key(inv)]
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


@contextmanager
def _held_partial(uploads: Path, inv: UUID) -> Iterator[Path]:
    """A ``<token>.<uuid>.partial`` under an exclusive ``flock``, as a live fetcher holds it."""
    inv_dir = uploads / str(inv)
    inv_dir.mkdir(parents=True, exist_ok=True)
    partial = inv_dir / f"{_TOKEN}.{uuid4().hex}.partial"
    partial.write_bytes(b"a detached download is still writing this")
    fd = os.open(partial, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield partial
    finally:
        os.close(fd)


def test_a_live_partial_survives_the_torn_down_ordering_and_retains_the_marker(
    migrated_url: str, tmp_path: Path
) -> None:
    # #1544 end to end, on the exact ordering that falsifies the old derivation. The System that
    # requested this base is torn_down, so _ROOTFS_REFERENCERS_SQL does not even consider it, the
    # gate reclaims the last rootfs row, and the drain tail sweeps — while the detached,
    # uncancellable download is still writing its partial. Before ADR-0452 the partial was unlinked
    # here and the fetcher died at os.replace.
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

        with _held_partial(uploads, inv) as live:
            result = await _run_handler(
                migrated_url, inv, [artifact_id], store, rootfs_dir, uploads
            )
            assert result == "1"
            assert live.exists(), "the reclaim sweep destroyed a live fetcher's partial"
            assert live.read_bytes() == b"a detached download is still writing this"

        assert not staged.exists()  # the committed base is still reclaimed
        assert store.deleted == [_object_key(inv)]
        assert (uploads / str(inv)).exists()  # ENOTEMPTY: the held partial keeps the dir
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
            # Retained deliberately: the marker is the only thing that re-enqueues a reclaim for a
            # closed investigation, so clearing it here would leave the skipped partial with no
            # collector if its holder is then killed (ADR-0452 §4).
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

    asyncio.run(_run())


def test_an_unopenable_partial_still_clears_the_marker(migrated_url: str, tmp_path: Path) -> None:
    # ADR-0452 §4's other half: an EACCES partial is permanent until an operator acts, so pinning
    # the marker on it would resurrect ADR-0442's never-clearing marker and its re-fail-every-pass
    # loop. Only a held flock — released by the kernel on process exit — retains the marker.
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
        unopenable = inv_dir / f"{_TOKEN}.deadbeef.partial"
        unopenable.write_bytes(b"present but not openable by this uid")
        real_open = os.open

        def refusing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(path) == unopenable:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", refusing_open)
            result = await _run_handler(
                migrated_url, inv, [], _RecordingStore(), rootfs_dir, uploads
            )
        assert result == "0"

        assert unopenable.exists()  # left for an operator rather than unlinked unchecked
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


def test_a_base_published_after_its_row_was_reclaimed_is_collected(
    migrated_url: str, tmp_path: Path
) -> None:
    # ADR-0452 §6, at the handler. The deferred pass that runs once the live writer is gone finds
    # the base it published onto a path whose artifacts row this reclaim already deleted, collects
    # it, removes the drained directory, and only then clears the marker. Without this the flock
    # gate would trade a destroyed download for a permanent SENSITIVE base nothing else reclaims.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        orphan_base = _stage(uploads, inv)  # no artifacts row owns it

        assert await _run_handler(
            migrated_url, inv, [], _RecordingStore(), rootfs_dir, uploads
        ) == ("0")

        assert not orphan_base.exists()
        assert not (uploads / str(inv)).exists()
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


def _orphan_base(uploads: Path, inv: UUID) -> tuple[Path, Path]:
    """A staged base of some *other* checksum with no artifacts row, plus its ADR-0451 marker.

    The #1559 shape: a fetcher published onto a path whose row had already been reclaimed, or a
    worker died between the publish and the row commit. Nothing in the tree owns it.
    """
    orphan = uploads / str(inv) / f"{rootfs_object_token(_CHECKSUM_Y)}.qcow2"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"published with no row to own it")
    marker = staged_rootfs_marker_path(orphan)
    marker.touch()
    return orphan, marker


def test_an_orphan_base_is_collected_even_though_a_faulting_checksum_keeps_its_row(
    migrated_url: str, tmp_path: Path
) -> None:
    # Residual (c) of #1559. A store that refuses the delete keeps the checksum's row, and
    # ADR-0442's drain tail returned early on any surviving row -- so the staging directory was
    # never swept again and an orphan base beside it was collected by nothing, indefinitely.
    # ADR-0494 keys the
    # collection on each file's own token, so the orphan goes while the faulting row's own base and
    # marker (already unlinked here, ahead of the failed store delete) are untouched.
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
        _stage(uploads, inv)
        orphan, orphan_marker = _orphan_base(uploads, inv)
        store = _RecordingStore(fail_on=_object_key(inv))

        with pytest.raises(CategorizedError):
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads)

        assert not orphan.exists(), "the orphan base outlived a pass that could see it"
        assert not orphan_marker.exists()
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)  # the faulting row is still retained ...
            assert await _marker(check, inv) is not None  # ... and so is the drain marker
        finally:
            await check.close()

    asyncio.run(_run())


def test_a_live_pinned_base_keeps_its_marker_while_an_orphan_beside_it_is_collected(
    migrated_url: str, tmp_path: Path
) -> None:
    # The other half of running the sweep alongside surviving rows: the pinned base is the
    # expected steady state for the whole grace window, so an unconditional marker glob would
    # strip the ADR-0451 completion marker off a perfectly good base every pass -- making the
    # reuse gate reject it and re-download it, silently, since the re-stage succeeds.
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
        (rootfs_dir / overlay_name(str(sys_id))).write_bytes(b"overlay")  # pins the base
        staged = _stage(uploads, inv)
        orphan, orphan_marker = _orphan_base(uploads, inv)
        store = _RecordingStore()

        assert (
            await _run_handler(migrated_url, inv, [artifact_id], store, rootfs_dir, uploads) == "0"
        )

        assert staged.exists()
        assert staged_rootfs_marker_path(staged).exists(), "a live base lost its completion marker"
        assert not orphan.exists()
        assert not orphan_marker.exists()
        assert store.deleted == []
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

    asyncio.run(_run())


def test_an_orphan_base_pinned_by_a_live_system_is_left_in_place(
    migrated_url: str, tmp_path: Path
) -> None:
    # ADR-0494 section 3's liveness half. A base whose row was reclaimed can still be backed by
    # a per-System overlay created after that reclaim, the residue ADR-0452 section 6 recorded
    # and could not see -- the zero-row precondition reads rows and the overlay is a file.
    # Unlinking
    # it pulls the backing file out from under a running guest, so the sweep leaves it and retains
    # the drain marker instead.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            sys_id = await _seed_system(seed, inv, "ready", _upload_profile(_CHECKSUM_Y))
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        (rootfs_dir / overlay_name(str(sys_id))).write_bytes(b"overlay")
        orphan, orphan_marker = _orphan_base(uploads, inv)
        collectable = uploads / str(inv) / f"{_TOKEN}.qcow2"
        collectable.write_bytes(b"neither owned nor pinned")

        assert (
            await _run_handler(migrated_url, inv, [], _RecordingStore(), rootfs_dir, uploads) == "0"
        )

        assert orphan.exists(), "a base under a live overlay was unlinked"
        assert orphan_marker.exists()
        assert not collectable.exists()  # ... while an unpinned orphan beside it still goes
        check = await connect(migrated_url)
        try:
            # The marker still clears: a pin by a failed System never heals, so retaining on it is
            # the never-clearing marker ADR-0442 was written about. An open/active investigation is
            # revisited by the staging-drain lane, which is not marker-keyed.
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())


def test_an_orphan_base_drains_a_closed_investigation_that_has_no_rows_left(
    migrated_url: str, tmp_path: Path
) -> None:
    # The empty-worklist path both the close-driven lane and ADR-0494's new staging-drain lane use:
    # the reclaim loop has nothing to do, and the whole job is the drain tail. The orphan goes, the
    # directory goes, and the marker clears.
    inv = uuid4()

    async def _run() -> None:
        nonlocal inv
        seed = await connect(migrated_url)
        try:
            inv = await _seed_investigation(seed, state="closed", closed=True)
            await _seed_system(seed, inv, "torn_down", _upload_profile(_CHECKSUM_Y))
        finally:
            await seed.close()
        rootfs_dir, uploads = _dirs(tmp_path)
        orphan, orphan_marker = _orphan_base(uploads, inv)

        assert (
            await _run_handler(migrated_url, inv, [], _RecordingStore(), rootfs_dir, uploads) == "0"
        )

        assert not orphan.exists()
        assert not orphan_marker.exists()
        assert not orphan.parent.exists()
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is None
        finally:
            await check.close()

    asyncio.run(_run())
