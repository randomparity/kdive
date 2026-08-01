"""Crash and reverse-fence proofs for image publication recovery (ADR-0526)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    HeadResult,
    ObjectListing,
    StoredArtifact,
)
from kdive.db.locks import LockScope, _lock_key
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.images import repair_dangling_images
from kdive.services.images.publication_fence import publication_fence
from kdive.services.images.publish import (
    PublishRequest,
    PublishReservation,
    finish_publish,
    publish_image,
    reserve_publish,
    write_publish_object,
)
from kdive.services.images.retention import repair_expired_private_images

_GRACE = timedelta(hours=1)
_TIMEOUT = 10.0


class _BlockingPutStore:
    """In-memory store whose first PUT is a bounded cross-thread barrier."""

    def __init__(self, *, block_first_put: bool = True) -> None:
        self.put_entered = threading.Event()
        self.put_release = threading.Event()
        self.put_finished = threading.Event()
        self._block_first_put = block_first_put
        self._lock = threading.Lock()
        self._objects: dict[str, tuple[bytes, str | None]] = {}
        self.puts: list[str] = []
        self.deleted: list[str] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        with self._lock:
            should_block = self._block_first_put
            self._block_first_put = False
            self.puts.append(key)
        if should_block:
            self.put_entered.set()
        try:
            if should_block and not self.put_release.wait(_TIMEOUT):
                raise TimeoutError("test PUT barrier was not released")
            with self._lock:
                self._objects[key] = (request.data, request.sha256_b64)
            return StoredArtifact(
                key=key,
                etag="etag",
                sensitivity=request.sensitivity,
                retention_class=request.retention_class,
            )
        finally:
            if should_block:
                self.put_finished.set()

    def head(self, key: str) -> HeadResult | None:
        with self._lock:
            stored = self._objects.get(key)
        if stored is None:
            return None
        data, checksum = stored
        return HeadResult(
            size_bytes=len(data),
            checksum_sha256=checksum,
            etag="etag",
            last_modified=datetime.now(UTC),
        )

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)
            self.deleted.append(key)

    def list_image_objects(self) -> list[ObjectListing]:
        with self._lock:
            keys = tuple(self._objects)
        return [ObjectListing(key=key, last_modified=datetime.now(UTC)) for key in keys]

    def head_present(self, key: str) -> bool:
        return self.head(key) is not None

    def seed_object(self, key: str, data: bytes, checksum: str | None) -> None:
        with self._lock:
            self._objects[key] = (data, checksum)


class _BlockingHeadStore(_BlockingPutStore):
    """Store whose first HEAD is a bounded barrier after recovery locks are held."""

    def __init__(self) -> None:
        super().__init__(block_first_put=False)
        self.head_entered = threading.Event()
        self.head_release = threading.Event()
        self.head_finished = threading.Event()
        self._block_first_head = True

    def head(self, key: str) -> HeadResult | None:
        with self._lock:
            should_block = self._block_first_head
            self._block_first_head = False
        if should_block:
            self.head_entered.set()
            try:
                if not self.head_release.wait(_TIMEOUT):
                    raise TimeoutError("test HEAD barrier was not released")
            finally:
                self.head_finished.set()
        return super().head(key)


def _publish_request(
    name: str,
    data: bytes,
    *,
    visibility: ImageVisibility = ImageVisibility.PUBLIC,
    owner: str | None = None,
    expires_at: datetime | None = None,
) -> PublishRequest:
    return PublishRequest(
        provider="local-libvirt",
        name=name,
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
        capabilities=(),
        provenance={},
        visibility=visibility,
        owner=owner,
        expires_at=expires_at,
    )


async def _connect(url: str, *, autocommit: bool) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=autocommit)


async def _wait_for_event(event: threading.Event) -> None:
    reached = await asyncio.wait_for(asyncio.to_thread(event.wait, _TIMEOUT), _TIMEOUT)
    assert reached


async def _pending_row(conn: psycopg.AsyncConnection) -> tuple[UUID, str]:
    cur = await conn.execute("SELECT id, object_key FROM image_catalog WHERE state = 'pending'")
    row = await cur.fetchone()
    assert row is not None
    assert isinstance(row[0], UUID)
    assert isinstance(row[1], str)
    return row[0], row[1]


async def _age_row(conn: psycopg.AsyncConnection, row_id: UUID) -> None:
    await conn.execute(
        "UPDATE image_catalog SET pending_since = now() - interval '2 hours' WHERE id = %s",
        (row_id,),
    )


def _lock_parts(row_id: UUID) -> tuple[int, int]:
    key = _lock_key(LockScope.IMAGE_PUBLISH, row_id) & 0xFFFF_FFFF_FFFF_FFFF
    return (key >> 32) & 0xFFFF_FFFF, key & 0xFFFF_FFFF


async def _assert_no_publication_lock(url: str, row_id: UUID) -> None:
    classid, objid = _lock_parts(row_id)
    probe = await _connect(url, autocommit=True)
    try:
        cur = await probe.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s AND objid = %s AND objsubid = 1",
            (classid, objid),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        await probe.close()


async def _publication_waiter_exists(conn: psycopg.AsyncConnection, row_id: UUID) -> bool:
    classid, objid = _lock_parts(row_id)
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
        "AND classid = %s AND objid = %s AND objsubid = 1 AND NOT granted)",
        (classid, objid),
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _wait_for_publication_waiter(conn: psycopg.AsyncConnection, row_id: UUID) -> None:
    async def _poll() -> None:
        while not await _publication_waiter_exists(conn, row_id):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), _TIMEOUT)


async def _row_waiter_exists(conn: psycopg.AsyncConnection, backend_pid: int) -> bool:
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted "
        "AND locktype IN ('transactionid', 'tuple'))",
        (backend_pid,),
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _wait_for_row_waiter(conn: psycopg.AsyncConnection, backend_pid: int) -> None:
    async def _poll() -> None:
        while not await _row_waiter_exists(conn, backend_pid):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), _TIMEOUT)


async def _contend_on_image_row(conn: psycopg.AsyncConnection, row_id: UUID) -> int:
    cur = await conn.execute(
        "UPDATE image_catalog SET updated_at = updated_at WHERE id = %s", (row_id,)
    )
    return cur.rowcount


async def _settle(task: asyncio.Task[object]) -> None:
    """Bound cancellation and retrieve terminal errors without hiding a timeout."""
    if not task.done():
        task.cancel()
        await asyncio.wait_for(asyncio.wait((task,)), _TIMEOUT)
    assert task.done()
    if not task.cancelled():
        task.exception()


def test_settle_propagates_nonterminating_task_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup must fail when cancellation does not terminate a task within its bound."""

    async def _run() -> None:
        async def _slow_cancellation() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)

        task = asyncio.create_task(_slow_cancellation())
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            await _settle(task)
        assert not task.done()
        await asyncio.wait_for(task, 1.0)
        assert task.done()

    monkeypatch.setattr("tests.adversarial.test_image_publication_recovery._TIMEOUT", 0.01)
    asyncio.run(_run())


async def _install_delete_failure_trigger(
    conn: psycopg.AsyncConnection,
    row_id: UUID,
    trigger_name: str,
    function_name: str,
) -> None:
    await conn.execute(
        sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF OLD.id = {}::uuid THEN "
            "RAISE EXCEPTION 'injected post-object-delete rollback'; "
            "END IF; RETURN OLD; END $$"
        ).format(sql.Identifier(function_name), sql.Literal(str(row_id)))
    )
    await conn.execute(
        sql.SQL(
            "CREATE TRIGGER {} BEFORE DELETE ON image_catalog FOR EACH ROW EXECUTE FUNCTION {}()"
        ).format(sql.Identifier(trigger_name), sql.Identifier(function_name))
    )


def _delete_failure_trigger_names() -> tuple[str, str]:
    suffix = uuid4().hex
    return f"fail_image_delete_trigger_{suffix}", f"fail_image_delete_{suffix}"


async def _remove_delete_failure_trigger(
    conn: psycopg.AsyncConnection, trigger_name: str, function_name: str
) -> None:
    await conn.execute(
        sql.SQL("DROP TRIGGER IF EXISTS {} ON image_catalog").format(sql.Identifier(trigger_name))
    )
    await conn.execute(
        sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(function_name))
    )


def test_trigger_names_allow_cleanup_when_trigger_creation_fails(
    migrated_url: str,
) -> None:
    """Caller-owned names make partial trigger installation recoverable."""

    async def _run() -> None:
        suffix = uuid4().hex
        trigger_name = f"fail_image_delete_trigger_{suffix}"
        function_name = f"fail_image_delete_{suffix}"
        collision_function = f"existing_image_delete_{suffix}"
        conn = await _connect(migrated_url, autocommit=True)
        try:
            await conn.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RETURN OLD; END $$"
                ).format(sql.Identifier(collision_function))
            )
            await conn.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE DELETE ON image_catalog "
                    "FOR EACH ROW EXECUTE FUNCTION {}()"
                ).format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(collision_function),
                )
            )
            with pytest.raises(psycopg.errors.DuplicateObject):
                await _install_delete_failure_trigger(
                    conn,
                    uuid4(),
                    trigger_name,
                    function_name,
                )
        finally:
            await _remove_delete_failure_trigger(conn, trigger_name, function_name)
            await conn.execute(
                sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(collision_function))
            )
            await conn.close()

        check = await _connect(migrated_url, autocommit=True)
        try:
            cur = await check.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = %s)",
                (function_name,),
            )
            assert await cur.fetchone() == (False,)
            cur = await check.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = %s)",
                (trigger_name,),
            )
            assert await cur.fetchone() == (False,)
        finally:
            await check.close()

    asyncio.run(_run())


async def _publish_reserved(
    conn: psycopg.AsyncConnection,
    store: _BlockingPutStore,
    reservation: PublishReservation,
    source: Path,
) -> object:
    async with publication_fence(conn, reservation):
        config_written = await write_publish_object(store, reservation, source)
        async with conn.transaction():
            return await finish_publish(conn, reservation, config_written=config_written)


def test_active_slow_publisher_is_skipped_until_its_fence_releases(
    migrated_url: str, tmp_path: Path
) -> None:
    """Recovery skips a live PUT, then reaches a terminal outcome after lock release."""

    async def _run() -> None:
        data = b"slow-publication"
        source = tmp_path / "slow.qcow2"
        source.write_bytes(data)
        store = _BlockingPutStore()
        publisher_conn = await _connect(migrated_url, autocommit=True)
        recovery_conn = await _connect(migrated_url, autocommit=False)
        observer_conn = await _connect(migrated_url, autocommit=True)
        publisher = asyncio.create_task(
            publish_image(
                publisher_conn,
                store,
                request=_publish_request("slow", data),
                source=source,
            )
        )
        row_id: UUID | None = None
        try:
            await _wait_for_event(store.put_entered)
            row_id, object_key = await _pending_row(observer_conn)
            await _age_row(observer_conn, row_id)

            skipped = await asyncio.wait_for(
                repair_dangling_images(recovery_conn, store, _GRACE), _TIMEOUT
            )
            assert skipped == 0
            assert not publisher.done()

            store.put_release.set()
            published = await asyncio.wait_for(publisher, _TIMEOUT)
            assert published.state.value == "registered"
            await _wait_for_event(store.put_finished)

            store.delete(object_key)
            await _age_row(observer_conn, row_id)
            terminal = await asyncio.wait_for(
                repair_dangling_images(recovery_conn, store, _GRACE), _TIMEOUT
            )
            assert terminal == 1
            cur = await observer_conn.execute(
                "SELECT 1 FROM image_catalog WHERE id = %s", (row_id,)
            )
            assert await cur.fetchone() is None
        finally:
            store.put_release.set()
            if not publisher.done():
                await publisher_conn.close()
            await _settle(publisher)
            if store.put_entered.is_set():
                await _wait_for_event(store.put_finished)
            await publisher_conn.close()
            await recovery_conn.close()
            await observer_conn.close()
            if row_id is not None:
                await _assert_no_publication_lock(migrated_url, row_id)

    asyncio.run(_run())


def test_recovery_fence_blocks_publisher_before_revalidation_and_put(
    migrated_url: str, tmp_path: Path
) -> None:
    """Recovery wins the reverse race and the stale publisher exits before PUT."""

    async def _run() -> None:
        data = b"reverse-fence"
        source = tmp_path / "reverse.qcow2"
        source.write_bytes(data)
        store = _BlockingHeadStore()
        publisher_conn = await _connect(migrated_url, autocommit=True)
        recovery_conn = await _connect(migrated_url, autocommit=False)
        observer_conn = await _connect(migrated_url, autocommit=True)
        row_contender_conn = await _connect(migrated_url, autocommit=True)
        reservation = await reserve_publish(
            publisher_conn,
            _publish_request("reverse", data),
            size_bytes=len(data),
        )
        await _age_row(observer_conn, reservation.row_id)
        recovery = asyncio.create_task(repair_dangling_images(recovery_conn, store, _GRACE))
        publisher: asyncio.Task[object] | None = None
        row_contender: asyncio.Task[object] | None = None
        try:
            await _wait_for_event(store.head_entered)
            row_contender = asyncio.create_task(
                _contend_on_image_row(row_contender_conn, reservation.row_id)
            )
            await _wait_for_row_waiter(observer_conn, row_contender_conn.info.backend_pid)
            publisher = asyncio.create_task(
                _publish_reserved(publisher_conn, store, reservation, source)
            )
            await _wait_for_publication_waiter(observer_conn, reservation.row_id)
            assert store.puts == []

            store.head_release.set()
            assert await asyncio.wait_for(recovery, _TIMEOUT) == 1
            assert await asyncio.wait_for(row_contender, _TIMEOUT) == 0
            with pytest.raises(CategorizedError) as error:
                await asyncio.wait_for(publisher, _TIMEOUT)
            assert error.value.category is ErrorCategory.CONFLICT
            assert store.puts == []
        finally:
            store.head_release.set()
            if not recovery.done():
                await recovery_conn.close()
            await _settle(recovery)
            if row_contender is not None:
                if not row_contender.done():
                    await row_contender_conn.close()
                await _settle(row_contender)
            if publisher is not None:
                if not publisher.done():
                    await publisher_conn.close()
                await _settle(publisher)
            if store.head_entered.is_set():
                await _wait_for_event(store.head_finished)
            await publisher_conn.close()
            await recovery_conn.close()
            await observer_conn.close()
            await row_contender_conn.close()
            await _assert_no_publication_lock(migrated_url, reservation.row_id)

    asyncio.run(_run())


def test_cancelled_publisher_late_put_isolated_under_abandoned_attempt_key(
    migrated_url: str, tmp_path: Path
) -> None:
    """Cancellation/session loss cannot let late bytes overwrite a successor attempt."""

    async def _run() -> None:
        data = b"late-publication"
        source = tmp_path / "late.qcow2"
        source.write_bytes(data)
        store = _BlockingPutStore()
        publisher_conn = await _connect(migrated_url, autocommit=True)
        recovery_conn = await _connect(migrated_url, autocommit=False)
        observer_conn = await _connect(migrated_url, autocommit=True)
        successor_conn = await _connect(migrated_url, autocommit=True)
        publisher = asyncio.create_task(
            publish_image(
                publisher_conn,
                store,
                request=_publish_request("late", data),
                source=source,
            )
        )
        abandoned_id: UUID | None = None
        successor_id: UUID | None = None
        try:
            await _wait_for_event(store.put_entered)
            abandoned_id, abandoned_key = await _pending_row(observer_conn)
            await _age_row(observer_conn, abandoned_id)

            publisher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(publisher, _TIMEOUT)
            await publisher_conn.close()

            assert (
                await asyncio.wait_for(
                    repair_dangling_images(recovery_conn, store, _GRACE), _TIMEOUT
                )
                == 1
            )
            successor = await asyncio.wait_for(
                publish_image(
                    successor_conn,
                    store,
                    request=_publish_request("late", data),
                    source=source,
                ),
                _TIMEOUT,
            )
            successor_id = successor.id
            assert successor.object_key is not None
            assert successor.object_key != abandoned_key
            assert store.head(abandoned_key) is None

            store.put_release.set()
            await _wait_for_event(store.put_finished)
            assert store.head(abandoned_key) is not None
            assert store.head(successor.object_key) is not None
            cur = await observer_conn.execute(
                "SELECT id, object_key FROM image_catalog WHERE name = 'late'"
            )
            assert await cur.fetchone() == (successor.id, successor.object_key)
        finally:
            store.put_release.set()
            if not publisher.done():
                await publisher_conn.close()
            await _settle(publisher)
            if store.put_entered.is_set():
                await _wait_for_event(store.put_finished)
            await publisher_conn.close()
            await recovery_conn.close()
            await observer_conn.close()
            await successor_conn.close()
            if abandoned_id is not None:
                await _assert_no_publication_lock(migrated_url, abandoned_id)
            if successor_id is not None:
                await _assert_no_publication_lock(migrated_url, successor_id)

    asyncio.run(_run())


def test_delete_rollback_preserves_pending_row_for_next_recovery_pass(
    migrated_url: str,
) -> None:
    """A rollback after object deletion leaves a missing-object row the next pass removes."""

    async def _run() -> None:
        expected = b"expected-publication"
        store = _BlockingPutStore(block_first_put=False)
        seed_conn = await _connect(migrated_url, autocommit=True)
        recovery_conn = await _connect(migrated_url, autocommit=False)
        observer_conn = await _connect(migrated_url, autocommit=True)
        reservation = await reserve_publish(
            seed_conn,
            _publish_request("rollback", expected),
            size_bytes=len(expected),
        )
        await _age_row(observer_conn, reservation.row_id)
        store.seed_object(
            reservation.object_key,
            b"invalid-publication",
            base64.b64encode(b"x" * 32).decode("ascii"),
        )
        trigger = _delete_failure_trigger_names()
        try:
            await _install_delete_failure_trigger(
                observer_conn,
                reservation.row_id,
                trigger[0],
                trigger[1],
            )
            try:
                with pytest.raises(
                    psycopg.errors.RaiseException,
                    match="injected post-object-delete rollback",
                ):
                    await asyncio.wait_for(
                        repair_dangling_images(recovery_conn, store, _GRACE),
                        _TIMEOUT,
                    )
                assert store.deleted == [reservation.object_key]
                assert store.head(reservation.object_key) is None
                cur = await observer_conn.execute(
                    "SELECT state FROM image_catalog WHERE id = %s",
                    (reservation.row_id,),
                )
                assert await cur.fetchone() == ("pending",)
            finally:
                await _remove_delete_failure_trigger(observer_conn, trigger[0], trigger[1])
                trigger = None

            assert (
                await asyncio.wait_for(
                    repair_dangling_images(recovery_conn, store, _GRACE), _TIMEOUT
                )
                == 1
            )
            cur = await observer_conn.execute(
                "SELECT 1 FROM image_catalog WHERE id = %s", (reservation.row_id,)
            )
            assert await cur.fetchone() is None
        finally:
            if trigger is not None:
                await _remove_delete_failure_trigger(observer_conn, trigger[0], trigger[1])
            await seed_conn.close()
            await recovery_conn.close()
            await observer_conn.close()
            await _assert_no_publication_lock(migrated_url, reservation.row_id)

    asyncio.run(_run())


def test_recovered_expired_private_image_is_pruned_on_next_ttl_pass(
    migrated_url: str,
) -> None:
    """Publication recovery owns pending; the following TTL pass owns registered expiry."""

    async def _run() -> None:
        data = b"expired-private-publication"
        store = _BlockingPutStore(block_first_put=False)
        seed_conn = await _connect(migrated_url, autocommit=True)
        recovery_conn = await _connect(migrated_url, autocommit=False)
        expiry_conn = await _connect(migrated_url, autocommit=False)
        observer_conn = await _connect(migrated_url, autocommit=True)
        reservation = await reserve_publish(
            seed_conn,
            _publish_request(
                "expired-private",
                data,
                visibility=ImageVisibility.PRIVATE,
                owner="proj",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
            size_bytes=len(data),
            principal="alice",
        )
        await _age_row(observer_conn, reservation.row_id)
        store.seed_object(
            reservation.object_key,
            data,
            base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
        )
        try:
            assert (
                await asyncio.wait_for(repair_expired_private_images(expiry_conn, store), _TIMEOUT)
                == 0
            )
            assert (
                await asyncio.wait_for(
                    repair_dangling_images(recovery_conn, store, _GRACE), _TIMEOUT
                )
                == 1
            )
            cur = await observer_conn.execute(
                "SELECT state FROM image_catalog WHERE id = %s", (reservation.row_id,)
            )
            assert await cur.fetchone() == ("registered",)
            assert store.head(reservation.object_key) is not None

            assert (
                await asyncio.wait_for(repair_expired_private_images(expiry_conn, store), _TIMEOUT)
                == 1
            )
            assert store.head(reservation.object_key) is None
            cur = await observer_conn.execute(
                "SELECT 1 FROM image_catalog WHERE id = %s", (reservation.row_id,)
            )
            assert await cur.fetchone() is None
        finally:
            await seed_conn.close()
            await recovery_conn.close()
            await expiry_conn.close()
            await observer_conn.close()
            await _assert_no_publication_lock(migrated_url, reservation.row_id)

    asyncio.run(_run())
