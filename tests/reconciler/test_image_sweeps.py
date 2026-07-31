"""Tests for the reconciler image sweeps (M2.4/6, ADR-0092/0093, issue #287).

Three deadline-guarded sweeps modeled on the upload reaper:

* ``repair_leaked_images`` — an object under the image prefix with **no catalog row**,
  older than the publish grace (keyed off the object's store mtime), is deleted. A
  ``pending`` row inside its deadline protects its object (the row-first publish window).
* ``repair_dangling_images`` — a row whose object HEAD is missing **past its publish
  deadline** has its row removed; an object-less ``defined`` baseline is skipped (it is
  object-less by design, not dangling).
* ``repair_expired_private_images`` — a ``private`` row with ``expires_at < now()`` has
  its object + row deleted, but is **reference-guarded** (an image a non-terminal System
  still references via ``provisioning_profile`` is skipped) and **extend-fenced** (the
  ``expires_at`` is re-read under a per-row lock so a concurrent extend is honored).

Seeding uses an autocommit ``connect`` connection; repairs run through a real
non-autocommit pool via ``run_repair`` (mirroring ``test_upload_reaper.py``). All time
windows are set in SQL against the Postgres clock, so there is no test-vs-DB skew.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.reconciler.cleanup.images as image_cleanup
from kdive.artifacts.storage import ArtifactWriteRequest, HeadResult, StoredArtifact
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.images import (
    ImageMtime,
    _delete_if_leaked,
)
from kdive.reconciler.cleanup.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.cleanup.images import (
    repair_leaked_images as _repair_leaked_images,
)
from kdive.services.images.retention import (
    expire_one_private_image as _expire_one_private_image,
)
from kdive.services.images.retention import (
    repair_expired_private_images as _repair_expired_private_images,
)
from kdive.services.images.upload import _project_usage
from tests.clock import STORE_MTIME
from tests.reconciler.conftest import connect, run_repair, seed_system


class _FakeImageStore:
    """A narrow image-sweep store stand-in (structural match for the repair port).

    ``objects`` maps object key -> age (how long ago the object was written). ``head``
    reports presence; ``list_image_objects`` reports each key with a Postgres-relative
    ``ImageMtime`` so the leaked-grace comparison stays on the DB clock.
    """

    def __init__(
        self,
        objects: dict[str, timedelta],
        *,
        fails_on: frozenset[str] = frozenset(),
        heads: dict[str, HeadResult] | None = None,
        head_errors: set[str] | None = None,
        delete_errors: set[str] | None = None,
        sticky_deletes: set[str] | None = None,
    ) -> None:
        # objects maps key -> age; the absolute mtime is now - age.
        self._objects = dict(objects)
        self._heads = dict(heads or {})
        self._head_errors = set(head_errors or ())
        self._delete_errors = set(delete_errors or ())
        self._sticky_deletes = set(sticky_deletes or ())
        self._now = datetime.now(UTC)
        self.deleted: list[str] = []
        self.deleted_versions: list[tuple[str, str]] = []
        self._fails_on = fails_on

    def list_image_objects(self) -> list[ImageMtime]:
        return [
            ImageMtime(key=key, last_modified=self._now - age)
            for key, age in self._objects.items()
            if key not in self.deleted
        ]

    def head_present(self, key: str) -> bool:
        return self.head(key) is not None

    def head(self, key: str) -> HeadResult | None:
        if key in self._head_errors:
            raise RuntimeError(f"HEAD failed for {key}")
        if key not in self._objects or (key in self.deleted and key not in self._sticky_deletes):
            return None
        return self._heads.get(
            key,
            HeadResult(
                size_bytes=1,
                checksum_sha256=None,
                etag="etag",
                last_modified=self._now - self._objects[key],
                version_id="test-version",
            ),
        )

    def delete_version(self, key: str, version_id: str) -> None:
        if key in self._fails_on:
            raise CategorizedError("delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        if key in self._delete_errors:
            raise RuntimeError(f"delete failed for {key}")
        self.deleted.append(key)
        self.deleted_versions.append((key, version_id))

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        self.deleted.append(key)
        return True

    def put_artifact(
        self, request: ArtifactWriteRequest
    ) -> StoredArtifact:  # pragma: no cover - sweeps never upload
        return StoredArtifact(
            key=request.key(),
            etag="etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
            version_id="test-version",
        )


class _PeerPutImageStore(_FakeImageStore):
    """Records a selected HEAD and exact deletion around a simulated peer replacement."""

    def __init__(self, objects: dict[str, timedelta]) -> None:
        super().__init__(objects)
        self.events: list[str] = []
        self.current_versions: dict[str, str] = {}

    def head(self, key: str) -> HeadResult | None:
        head = super().head(key)
        if head is None:
            return None
        self.events.append("head")
        return head

    def delete_version(self, key: str, version_id: str) -> None:
        self.events.append("delete_version")
        super().delete_version(key, version_id)


class _FenceCursor:
    def __init__(self, store: _PeerPutImageStore, key: str) -> None:
        self._store = store
        self._key = key

    async def __aenter__(self) -> _FenceCursor:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> None:
        return None

    async def fetchone(self) -> tuple[bool]:
        self._store.events.append("row")
        self._store.current_versions[self._key] = "peer-version-2"
        return (False,)


class _FenceConnection:
    def __init__(self, store: _PeerPutImageStore, key: str) -> None:
        self._store = store
        self._key = key

    def cursor(self) -> _FenceCursor:
        return _FenceCursor(self._store, self._key)


async def _insert_image_row(
    conn: psycopg.AsyncConnection,
    *,
    provider: str = "local-libvirt",
    name: str = "debian",
    arch: str = "x86_64",
    state: str = "registered",
    visibility: str = "public",
    object_key: str | None = "images/local-libvirt/debian/x86_64.qcow2",
    owner: str | None = None,
    pending_age: timedelta = timedelta(hours=2),
    expires_in: timedelta | None = None,
    kernel_config_key: str | None = None,
    digest: str | None = None,
    size_bytes: int = 0,
    publication_principal: str | None = None,
) -> UUID:
    """Insert one image_catalog row with DB-clock-relative pending_since/expires_at."""
    expires_clause = "now() + make_interval(secs => %(expires_secs)s)" if expires_in else "NULL"
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
        " visibility, owner, expires_at, state, size_bytes, publication_attempt_id, "
        "publication_principal, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, 'qcow2', '/dev/vda', %(object_key)s, "
        " %(kernel_config_key)s, %(digest)s, %(visibility)s, %(owner)s, "
        f"{expires_clause}, %(state)s, %(size_bytes)s, %(publication_attempt_id)s, "
        "%(publication_principal)s, "
        "now() - make_interval(secs => %(pending_secs)s)) "
        "RETURNING id",
        {
            "provider": provider,
            "name": name,
            "arch": arch,
            "object_key": object_key,
            "kernel_config_key": kernel_config_key,
            "digest": None if object_key is None else digest or "sha256:" + "a" * 64,
            "visibility": visibility,
            "owner": owner,
            "state": state,
            "size_bytes": size_bytes,
            "publication_attempt_id": uuid4() if state == "pending" else None,
            "publication_principal": publication_principal,
            "pending_secs": pending_age.total_seconds(),
            "expires_secs": (expires_in or timedelta()).total_seconds(),
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _set_catalog_rootfs(
    conn: psycopg.AsyncConnection, system_id: UUID, *, provider: str, name: str
) -> None:
    """Give a System a catalog-rootfs provisioning_profile referencing ``(provider, name)``."""
    profile = {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "kexec",
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "catalog", "provider": provider, "name": name}}
        },
    }
    await conn.execute(
        "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
        (Jsonb(profile), system_id),
    )


def _grace() -> timedelta:
    return timedelta(hours=1)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _head(data: bytes, *, checksum: str | None = None) -> HeadResult:
    return HeadResult(
        size_bytes=len(data),
        checksum_sha256=checksum,
        etag="etag",
        last_modified=STORE_MTIME,
        version_id="test-version",
    )


def _checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


# --- leaked_images -------------------------------------------------------------------


def test_leaked_object_past_grace_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/orphan/x86_64.qcow2"
        store = _FakeImageStore({key: timedelta(hours=2)})  # older than 1h grace, no row
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 1
        assert store.deleted == [key]

    asyncio.run(_run())


def test_leaked_object_inside_grace_is_protected(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/fresh/x86_64.qcow2"
        store = _FakeImageStore({key: timedelta(minutes=5)})  # inside 1h grace
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_leaked_sweep_protects_live_image_config(migrated_url: str) -> None:
    # A registered image's .config sibling is referenced via kernel_config_key, never object_key;
    # the sweep must protect it (ADR-0317) even though it lives under the images/ prefix.
    qcow2_key = "images/local-libvirt/debian/x86_64.qcow2"
    config_key = "images/local-libvirt/debian/x86_64.config"

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            await _insert_image_row(conn, object_key=qcow2_key, kernel_config_key=config_key)
        store = _FakeImageStore(
            {qcow2_key: timedelta(hours=2), config_key: timedelta(hours=2)}  # both past grace
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 0
        assert store.deleted == []  # both protected via object_key OR kernel_config_key

    asyncio.run(_run())


def test_leaked_sweep_reclaims_orphaned_config(migrated_url: str) -> None:
    # A .config object past grace that NO row references is reclaimed, exactly like an orphan qcow2.
    orphan_config = "images/local-libvirt/gone/x86_64.config"

    async def _run() -> None:
        store = _FakeImageStore({orphan_config: timedelta(hours=2)})  # no row references it
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 1
        assert store.deleted == [orphan_config]
        assert store.deleted_versions == [(orphan_config, "test-version")]

    asyncio.run(_run())


def test_leaked_sweep_continues_after_one_object_store_failure(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        failed = "images/local-libvirt/gone/x86_64.qcow2"
        later = "images/local-libvirt/later/x86_64.qcow2"
        store = _FakeImageStore(
            {failed: timedelta(hours=2), later: timedelta(hours=2)}, fails_on=frozenset({failed})
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))

        assert count == 1
        assert store.deleted_versions == [(later, "test-version")]

    with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.images"):
        asyncio.run(_run())
    assert any("gone/x86_64.qcow2" in record.getMessage() for record in caplog.records)


def test_leaked_sweep_deletes_the_pre_fence_head_version_after_a_peer_put(
    migrated_url: str,
) -> None:
    del migrated_url
    key = "images/local-libvirt/gone/x86_64.qcow2"
    store = _PeerPutImageStore({key: timedelta(hours=2)})
    obj = store.list_image_objects()[0]
    deleted = asyncio.run(
        _delete_if_leaked(
            cast(psycopg.AsyncConnection, _FenceConnection(store, key)), store, obj, _grace()
        )
    )

    assert deleted is True
    assert store.events == ["head", "row", "delete_version"]
    assert store.deleted_versions == [(key, "test-version")]
    assert store.current_versions[key] == "peer-version-2"


def test_pending_row_inside_deadline_protects_its_object(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/pub/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            # A pending publish in flight: row exists, written recently (inside grace).
            await _insert_image_row(
                seed,
                name="pub",
                state="pending",
                object_key=key,
                pending_age=timedelta(minutes=1),
            )
        store = _FakeImageStore({key: timedelta(hours=5)})  # object old, but a row owns it
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_multiple_leaked_objects_all_counted(migrated_url: str) -> None:
    # Two independent orphan objects past grace must each increment the deleted tally: the
    # return is the number of objects deleted, not a fixed 1.
    async def _run() -> None:
        keys = [f"images/local-libvirt/orphan{i}/x86_64.qcow2" for i in range(3)]
        store = _FakeImageStore({k: timedelta(hours=2) for k in keys})  # all past grace, no rows
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 3  # every leaked object counted, not a fixed 1
        assert sorted(store.deleted) == sorted(keys)

    asyncio.run(_run())


# --- dangling_images -----------------------------------------------------------------


def test_dangling_row_with_missing_object_past_deadline_is_removed(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/gone/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed, name="gone", object_key=key, pending_age=timedelta(hours=2)
            )
        store = _FakeImageStore({})  # object HEAD missing
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_object_less_defined_row_is_skipped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="baseline",
                state="defined",
                object_key=None,
                pending_age=timedelta(hours=5),
            )
        store = _FakeImageStore({})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None  # defined baseline survives

    asyncio.run(_run())


def test_dangling_row_inside_deadline_is_left_alone(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/young/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="young",
                state="pending",
                object_key=key,
                pending_age=timedelta(minutes=1),
            )
        store = _FakeImageStore({})  # object not landed yet, but inside deadline
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_dangling_skips_row_whose_object_is_present(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/healthy/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed, name="healthy", object_key=key, pending_age=timedelta(hours=2)
            )
        store = _FakeImageStore({key: timedelta(hours=2)})  # object present
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_multiple_dangling_rows_all_removed_and_counted(migrated_url: str) -> None:
    # Two dangling rows (objects missing past deadline) must each increment the removed tally.
    async def _run() -> None:
        row_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for i in range(3):
                row_ids.append(
                    await _insert_image_row(
                        seed,
                        name=f"gone{i}",
                        object_key=f"images/local-libvirt/gone{i}/x86_64.qcow2",
                        pending_age=timedelta(hours=2),
                    )
                )
        store = _FakeImageStore({})  # every object HEAD missing
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 3  # every dangling row removed, not a fixed 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT count(*) FROM image_catalog WHERE id = ANY(%s)", (row_ids,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == 0

    asyncio.run(_run())


def test_dangling_present_row_does_not_halt_subsequent_removal(migrated_url: str) -> None:
    # A row whose object is present is skipped, but the sweep must CONTINUE to the remaining
    # candidates (not break). The present row is seeded first so a `break` regression would drop
    # the dangling row that follows it.
    async def _run() -> None:
        present_key = "images/local-libvirt/healthy/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            present_id = await _insert_image_row(
                seed, name="healthy", object_key=present_key, pending_age=timedelta(hours=2)
            )
            missing_id = await _insert_image_row(
                seed,
                name="gone",
                object_key="images/local-libvirt/gone/x86_64.qcow2",
                pending_age=timedelta(hours=2),
            )
        store = _FakeImageStore({present_key: timedelta(hours=2)})  # only the present row's object
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 1  # the dangling row after the skip is still removed
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT id FROM image_catalog WHERE id = ANY(%s)", ([present_id, missing_id],)
            )
            surviving = {row[0] for row in await cur.fetchall()}
        assert surviving == {present_id}  # present row kept, dangling row gone

    asyncio.run(_run())


def test_abandoned_matching_publication_is_registered(migrated_url: str) -> None:
    data = b"complete-abandoned-publication"
    key = "images/local-libvirt/recovered/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="recovered",
                state="pending",
                object_key=key,
                digest=_digest(data),
                size_bytes=len(data),
            )
        store = _FakeImageStore(
            {key: timedelta(hours=2)}, heads={key: _head(data, checksum=_checksum(data))}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            terminal = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert terminal == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, publication_attempt_id, publication_principal "
                "FROM image_catalog WHERE id = %s",
                (row_id,),
            )
            assert await cur.fetchone() == ("registered", None, None)
        assert store.deleted == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("case", "digest", "head"),
    [
        ("wrong-size", _digest(b"expected"), _head(b"wrong-size", checksum=_checksum(b"expected"))),
        ("missing-checksum", _digest(b"expected"), _head(b"expected")),
        ("malformed-checksum", _digest(b"expected"), _head(b"expected", checksum="not-base64")),
        (
            "mismatched-checksum",
            _digest(b"expected"),
            _head(b"expected", checksum=_checksum(b"different")),
        ),
        ("malformed-digest", "sha256:abc", _head(b"expected", checksum=_checksum(b"expected"))),
    ],
)
def test_invalid_abandoned_publication_is_deleted(
    migrated_url: str, case: str, digest: str, head: HeadResult
) -> None:
    key = f"images/local-libvirt/{case}/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name=case,
                state="pending",
                object_key=key,
                digest=digest,
                size_bytes=len(b"expected"),
            )
        store = _FakeImageStore({key: timedelta(hours=2)}, heads={key: head})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            terminal = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert terminal == 1
        assert store.deleted == [key]
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


@pytest.mark.parametrize("config_present", [True, False])
def test_recovered_publication_reconciles_config_key(
    migrated_url: str, config_present: bool
) -> None:
    data = b"valid-with-optional-config"
    key = "images/local-libvirt/configured/x86_64/attempt.qcow2"
    config_key = "images/local-libvirt/configured/x86_64/attempt.config"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="configured",
                state="pending",
                object_key=key,
                kernel_config_key=config_key,
                digest=_digest(data),
                size_bytes=len(data),
            )
        objects = {key: timedelta(hours=2)}
        if config_present:
            objects[config_key] = timedelta(hours=2)
        store = _FakeImageStore(objects, heads={key: _head(data, checksum=_checksum(data))})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            terminal = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert terminal == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, kernel_config_key FROM image_catalog WHERE id = %s", (row_id,)
            )
            assert await cur.fetchone() == (
                "registered",
                config_key if config_present else None,
            )

    asyncio.run(_run())


def test_config_head_failure_keeps_publication_pending_for_retry(migrated_url: str) -> None:
    data = b"valid-but-config-head-fails"
    key = "images/local-libvirt/config-error/x86_64/attempt.qcow2"
    config_key = "images/local-libvirt/config-error/x86_64/attempt.config"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="config-error",
                state="pending",
                object_key=key,
                kernel_config_key=config_key,
                digest=_digest(data),
                size_bytes=len(data),
            )
        store = _FakeImageStore(
            {key: timedelta(hours=2), config_key: timedelta(hours=2)},
            heads={key: _head(data, checksum=_checksum(data))},
            head_errors={config_key},
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(RuntimeError, match="HEAD failed"):
                await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, kernel_config_key FROM image_catalog WHERE id = %s", (row_id,)
            )
            assert await cur.fetchone() == ("pending", config_key)

    asyncio.run(_run())


def test_recovered_private_publication_audits_persisted_principal(migrated_url: str) -> None:
    data = b"private-recovery"
    key = "images/local-libvirt__proj/private/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="private",
                state="pending",
                visibility="private",
                owner="proj",
                expires_in=timedelta(hours=1),
                object_key=key,
                digest=_digest(data),
                size_bytes=len(data),
                publication_principal="alice",
            )
        store = _FakeImageStore(
            {key: timedelta(hours=2)}, heads={key: _head(data, checksum=_checksum(data))}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert (
                await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace())) == 1
            )
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT principal, project, transition FROM audit_log WHERE object_id = %s",
                (row_id,),
            )
            assert await cur.fetchone() == ("alice", "proj", "private-upload:registered")

    asyncio.run(_run())


def test_missing_private_principal_reclaims_matching_object_and_quota(migrated_url: str) -> None:
    data = b"private-without-actor"
    key = "images/local-libvirt__proj/no-actor/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="no-actor",
                state="pending",
                visibility="private",
                owner="proj",
                expires_in=timedelta(hours=1),
                object_key=key,
                digest=_digest(data),
                size_bytes=len(data),
            )
            assert await _project_usage(seed, "proj", adopting=None) == (1, len(data))
        store = _FakeImageStore(
            {key: timedelta(hours=2)}, heads={key: _head(data, checksum=_checksum(data))}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert (
                await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace())) == 1
            )
        assert store.deleted == [key]
        async with await connect(migrated_url) as check:
            assert await _project_usage(check, "proj", adopting=None) == (0, 0)
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_private_recovery_audit_failure_rolls_back_registration(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"private-audit-failure"
    key = "images/local-libvirt__proj/audit-error/x86_64/attempt.qcow2"

    async def _fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(image_cleanup, "record_private_registration", _fail_audit, raising=False)

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="audit-error",
                state="pending",
                visibility="private",
                owner="proj",
                expires_in=timedelta(hours=1),
                object_key=key,
                digest=_digest(data),
                size_bytes=len(data),
                publication_principal="alice",
            )
        store = _FakeImageStore(
            {key: timedelta(hours=2)}, heads={key: _head(data, checksum=_checksum(data))}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(RuntimeError, match="audit unavailable"):
                await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() == ("pending",)
            cur = await check.execute("SELECT 1 FROM audit_log WHERE object_id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


@pytest.mark.parametrize("failure", ["delete-error", "still-present"])
def test_unproven_invalid_object_deletion_keeps_row_for_retry(
    migrated_url: str, failure: str
) -> None:
    data = b"invalid-object"
    key = f"images/local-libvirt/{failure}/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name=failure,
                state="pending",
                object_key=key,
                digest=_digest(data),
                size_bytes=len(data) + 1,
            )
        store = _FakeImageStore(
            {key: timedelta(hours=2)},
            heads={key: _head(data, checksum=_checksum(data))},
            delete_errors={key} if failure == "delete-error" else None,
            sticky_deletes={key} if failure == "still-present" else None,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            if failure == "delete-error":
                with pytest.raises(RuntimeError, match="delete failed"):
                    await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
            else:
                assert (
                    await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
                    == 0
                )
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() == ("pending",)

    asyncio.run(_run())


def test_repair_terminal_count_includes_registered_and_removed(migrated_url: str) -> None:
    valid = b"valid"
    valid_key = "images/local-libvirt/valid/x86_64/attempt.qcow2"
    invalid_key = "images/local-libvirt/invalid/x86_64/attempt.qcow2"
    missing_key = "images/local-libvirt/missing/x86_64/attempt.qcow2"

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _insert_image_row(
                seed,
                name="valid",
                state="pending",
                object_key=valid_key,
                digest=_digest(valid),
                size_bytes=len(valid),
            )
            await _insert_image_row(
                seed,
                name="invalid",
                state="pending",
                object_key=invalid_key,
                digest=_digest(b"expected"),
                size_bytes=len(b"expected"),
            )
            await _insert_image_row(
                seed,
                name="missing",
                state="pending",
                object_key=missing_key,
                digest=_digest(b"missing"),
                size_bytes=len(b"missing"),
            )
        store = _FakeImageStore(
            {valid_key: timedelta(hours=2), invalid_key: timedelta(hours=2)},
            heads={
                valid_key: _head(valid, checksum=_checksum(valid)),
                invalid_key: _head(b"wrong", checksum=_checksum(b"wrong")),
            },
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert (
                await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace())) == 3
            )

    asyncio.run(_run())


def test_dangling_repair_rejects_an_enclosing_transaction_before_store_access(
    migrated_url: str,
) -> None:
    key = "images/local-libvirt/nested/x86_64/attempt.qcow2"

    class _UntouchedStore(_FakeImageStore):
        def head(self, key: str) -> HeadResult | None:
            raise AssertionError(f"nested repair touched object store for {key}")

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            await _insert_image_row(
                conn,
                name="nested",
                state="pending",
                object_key=key,
                pending_age=timedelta(hours=2),
            )
            async with conn.transaction():
                with pytest.raises(RuntimeError, match="transaction-free connection"):
                    await _repair_dangling_images(conn, _UntouchedStore({}), _grace())

    asyncio.run(_run())


# --- expired_private_images ----------------------------------------------------------


def test_expired_private_image_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/priv/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="priv",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),  # already expired
            )
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 1
        assert store.deleted == [key]
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_multiple_expired_private_images_are_all_counted(migrated_url: str) -> None:
    async def _run() -> None:
        key_a = "images/local-libvirt__proj/priv-a/x86_64.qcow2"
        key_b = "images/local-libvirt__proj/priv-b/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            await _insert_image_row(
                seed,
                name="priv-a",
                visibility="private",
                owner="proj",
                object_key=key_a,
                expires_in=timedelta(seconds=-1),
            )
            await _insert_image_row(
                seed,
                name="priv-b",
                visibility="private",
                owner="proj",
                object_key=key_b,
                expires_in=timedelta(seconds=-1),
            )
        store = _FakeImageStore({key_a: timedelta(hours=2), key_b: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        # The pruned counter accumulates per row, so both expiries are reflected.
        assert count == 2
        assert sorted(store.deleted) == sorted([key_a, key_b])

    asyncio.run(_run())


def test_unexpired_private_image_is_kept(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/live/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="live",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(hours=1),  # still live
            )
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_expired_pending_private_image_is_not_an_expiry_candidate(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/publishing/x86_64/attempt.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="publishing",
                state="pending",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
                publication_principal="alice",
            )
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, lambda c: _repair_expired_private_images(c, store)) == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() == ("pending",)

    asyncio.run(_run())


def test_private_expiry_locked_reread_rejects_candidate_that_became_pending(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/rearmed/x86_64/attempt.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="rearmed",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            # Candidate selection saw registered. A publisher adopted it before the locked
            # re-read, so expiry must not delete the attempt's object or row.
            await conn.execute(
                "UPDATE image_catalog SET state = 'pending', publication_attempt_id = %s, "
                "publication_principal = 'alice' WHERE id = %s",
                (uuid4(), row_id),
            )
            store = _FakeImageStore({key: timedelta(hours=2)})
            assert await _expire_one_private_image(conn, store, row_id, key, None) is False
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() == ("pending",)

    asyncio.run(_run())


def test_expired_private_referenced_by_non_terminal_system_is_skipped(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/used/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="used",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            system_id = await seed_system(seed)  # READY, non-terminal
            await _set_catalog_rootfs(seed, system_id, provider="local-libvirt", name="used")
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None  # reference defers expiry

    asyncio.run(_run())


def test_expired_private_referenced_by_terminal_system_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        from kdive.domain.capacity.state import SystemState

        key = "images/local-libvirt__proj/dead/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="dead",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            await _set_catalog_rootfs(seed, system_id, provider="local-libvirt", name="dead")
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 1  # a terminal System does not defer expiry
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_concurrent_extend_under_lock_is_honored(migrated_url: str) -> None:
    """A candidate selected as expired but extended before the locked re-read is not deleted."""

    async def _run() -> None:
        key = "images/local-libvirt__proj/extended/x86_64.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="extended",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),  # candidate-eligible
            )
            # Simulate a concurrent operator extend committed before the per-row re-read.
            await conn.execute(
                "UPDATE image_catalog SET expires_at = now() + make_interval(hours => 1) "
                "WHERE id = %s",
                (row_id,),
            )
            store = _FakeImageStore({key: timedelta(hours=2)})
            deleted = await _expire_one_private_image(conn, store, row_id, key, None)
        assert deleted is False  # the re-read observes the extend
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_expire_one_private_image_deletes_when_still_expired(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/stale/x86_64.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="stale",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            store = _FakeImageStore({key: timedelta(hours=2)})
            deleted = await _expire_one_private_image(conn, store, row_id, key, None)
        assert deleted is True
        assert store.deleted == [key]

    asyncio.run(_run())


def test_private_expiry_deletes_config_object(migrated_url: str) -> None:
    # A private image's .config sibling is eagerly deleted alongside the qcow2 on expiry (ADR-0317).
    qcow2_key = "images/local-libvirt__proj/withcfg/x86_64.qcow2"
    config_key = "images/local-libvirt__proj/withcfg/x86_64.config"

    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            await _insert_image_row(
                conn,
                name="withcfg",
                visibility="private",
                owner="proj",
                object_key=qcow2_key,
                kernel_config_key=config_key,
                expires_in=timedelta(seconds=-1),
            )
        store = _FakeImageStore({qcow2_key: timedelta(hours=2), config_key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            pruned = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert pruned == 1
        assert {qcow2_key, config_key} <= set(store.deleted)

    asyncio.run(_run())


def test_expire_one_private_image_defers_when_referenced_under_lock(migrated_url: str) -> None:
    """The reference guard is evaluated inside the prune's row lock (atomic with the delete)."""

    async def _run() -> None:
        key = "images/local-libvirt__proj/locked/x86_64.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="locked",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            system_id = await seed_system(conn)  # non-terminal
            await _set_catalog_rootfs(conn, system_id, provider="local-libvirt", name="locked")
            store = _FakeImageStore({key: timedelta(hours=2)})
            deleted = await _expire_one_private_image(conn, store, row_id, key, None)
        assert deleted is False
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())
