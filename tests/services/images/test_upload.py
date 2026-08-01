"""Project-private upload registration (ADR-0093, ADR-0520, ADR-0525, ADR-0526).

``register_private_upload`` validates the quarantined object's guest contract, then — under the
project advisory lock — enforces the per-project count/bytes quota fail-closed and commits a
``pending`` row reserving this upload's bytes. The lock is released before the object-store write
(ADR-0520). These tests pin: a non-conforming image is rejected with a named reason while still
quarantined (never registered); an over-cap upload is denied fail-closed and audited; two
concurrent uploads cannot both pass either cap, on the committed reservation rather than a held
lock; **no PROJECT advisory lock is held while the PUT runs**; private finish is ordered after any
in-flight PROJECT-locked reservation without deadlocking its IMAGE_PUBLISH fence; an abandoned
reservation holds quota only until the reconciler's dangling sweep reclaims it; a registered
private image resolves only within its owning project and shadows a same-identity public image
there; and the publish refuses a connection that already opened a transaction, which would demote
its transaction to a savepoint that neither commits the reservation nor releases the lock
(ADR-0516 §1).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from kdive.artifacts import storage as artifact_types
from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_BYTES,
    IMAGE_PRIVATE_MAX_COUNT,
)
from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.images import ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.catalog import resolve_rootfs
from kdive.images.cataloging.validation import GUEST_CONTRACT_PATHS, InspectSeam
from kdive.security.audit import args_digest
from kdive.services.images.audit import record_private_registration
from kdive.services.images.upload import (
    PrivateUploadRequest,
    RegisteredPrivateNameConflict,
    _clamp_expiry,
    _project_usage,
    _quota_denial,
    _reject_oversize_upload,
    register_private_upload,
)
from tests.clock import STORE_MTIME

_REQUIRED = ("kdump", "drgn")
_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _conforming() -> InspectSeam:
    """An inspection seam reporting every guest-contract path as present."""

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return set(candidates)

    return _probe


def _missing(*absent: str) -> InspectSeam:
    """An inspection seam where the named contract elements are absent."""
    absent_paths = {GUEST_CONTRACT_PATHS[a] for a in absent}

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return {c for c in candidates if c not in absent_paths}

    return _probe


class _FakeStore:
    """In-memory store: get_artifact serves a seeded quarantined object; put/head mirror writes."""

    def __init__(self, quarantined: dict[str, bytes] | None = None) -> None:
        self._objects: dict[str, bytes] = dict(quarantined or {})
        self._checksums: dict[str, str | None] = {}
        self.puts: list[str] = []

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
        data = self._objects.get(key)
        if data is None:
            raise CategorizedError(
                f"artifact {key!r} is gone",
                category=ErrorCategory.STALE_HANDLE,
                details={"key": key},
            )
        return artifact_types.FetchedArtifact(data, Sensitivity.QUARANTINED, "upload")

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        key = request.key()
        self.puts.append(key)
        self._objects[key] = request.data
        self._checksums[key] = request.sha256_b64
        etag = hashlib.md5(request.data).hexdigest()  # noqa: S324 - etag stand-in, not security
        return artifact_types.StoredArtifact(
            key,
            etag,
            request.sensitivity,
            request.retention_class,
            version_id="test-version",
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(
            size_bytes=len(data),
            checksum_sha256=self._checksums.get(key),
            etag="etag",
            last_modified=STORE_MTIME,
            version_id="test-version",
        )


class _FirstPutGateStore(_FakeStore):
    """Pause the first published qcow2 PUT until the concurrency test releases it."""

    def __init__(
        self,
        quarantined: dict[str, bytes],
        *,
        first_put_started: threading.Event,
        release_first_put: threading.Event,
    ) -> None:
        super().__init__(quarantined)
        self._first_put_started = first_put_started
        self._release_first_put = release_first_put
        self._published_qcow2_count = 0
        self._put_count_lock = threading.Lock()

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        if request.key().endswith(".qcow2"):
            with self._put_count_lock:
                self._published_qcow2_count += 1
                is_first = self._published_qcow2_count == 1
            if is_first:
                self._first_put_started.set()
                if not self._release_first_put.wait(timeout=10):
                    raise AssertionError("test did not release the first published-object PUT")
        return super().put_artifact(request)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _connect_pooled_shape(url: str) -> psycopg.AsyncConnection:
    """Connect the way an MCP tool handler's connection arrives: **non**-autocommit.

    ``db/pool.py`` sets no ``autocommit``, so ``pool.connection()`` yields a connection on which
    one bare statement silently opens a transaction that lives until the pool takes it back
    (ADR-0506). The tests above use autocommit connections, where that state cannot arise.
    """
    return await psycopg.AsyncConnection.connect(url)


async def _advisory_locks_held_by(url: str, backend_pid: int) -> int:
    """Count advisory locks held by ``backend_pid``, probed from a **second** connection.

    Probing from the connection under test would itself issue the statement that opens the
    transaction being measured (ADR-0506).
    """
    async with await _connect(url) as probe, probe.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s",
            (backend_pid,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _scoped_lock_held_by(
    url: str, backend_pid: int, scope: LockScope, key_value: str
) -> bool:
    key = _lock_key(scope, key_value)
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    classid = (unsigned >> 32) & 0xFFFF_FFFF
    objid = unsigned & 0xFFFF_FFFF
    async with await _connect(url) as probe, probe.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = %s "
            "AND classid = %s AND objid = %s AND objsubid = 1 AND granted)",
            (backend_pid, classid, objid),
        )
        row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _pending_image_lock_held_by(url: str, backend_pid: int) -> bool:
    async with await _connect(url) as observer, observer.cursor() as cur:
        await cur.execute(
            "SELECT id FROM image_catalog "
            "WHERE owner = 'proj' AND state = 'pending' ORDER BY created_at"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    return await _scoped_lock_held_by(url, backend_pid, LockScope.IMAGE_PUBLISH, str(rows[0][0]))


async def _wait_for_pending_image_lock_waiter(url: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        async with await _connect(url) as observer, observer.cursor() as cur:
            await cur.execute("SELECT id FROM image_catalog WHERE state = 'pending'")
            row = await cur.fetchone()
            if row is not None:
                key = _lock_key(LockScope.IMAGE_PUBLISH, row[0])
                unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
                await cur.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = %s AND objid = %s AND objsubid = 1 AND NOT granted)",
                    ((unsigned >> 32) & 0xFFFF_FFFF, unsigned & 0xFFFF_FFFF),
                )
                waiting = await cur.fetchone()
                if waiting is not None and waiting[0]:
                    return
        await asyncio.sleep(0.02)
    raise AssertionError("second publisher never queued on the image publication fence")


async def _ungranted_advisory_locks(url: str) -> int:
    """Count advisory locks some backend is **blocked** on, probed from a second connection."""
    async with await _connect(url) as probe, probe.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _scoped_lock_has_waiter(url: str, scope: LockScope, key_value: str) -> bool:
    """Report whether a backend is blocked on the exact scoped advisory lock."""
    key = _lock_key(scope, key_value)
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    classid = (unsigned >> 32) & 0xFFFF_FFFF
    objid = unsigned & 0xFFFF_FFFF
    async with await _connect(url) as probe, probe.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s AND objid = %s AND objsubid = 1 AND NOT granted)",
            (classid, objid),
        )
        row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _wait_for_registered_row_or_project_waiter(url: str) -> str:
    """Wait until a publisher registers or blocks trying to order its finish on PROJECT."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if await _scoped_lock_has_waiter(url, LockScope.PROJECT, "proj"):
            return "project_waiter"
        async with await _connect(url) as observer, observer.cursor() as cur:
            await cur.execute(
                "SELECT EXISTS (SELECT 1 FROM image_catalog WHERE state = 'registered')"
            )
            row = await cur.fetchone()
        if row is not None and row[0]:
            return "registered"
        await asyncio.sleep(0.02)
    raise AssertionError("first publisher neither registered nor waited on the PROJECT lock")


async def _run_both_contending(url: str, *coro_factories) -> list[object]:
    """Run two uploads with both provably blocked on the PROJECT lock before either proceeds.

    `asyncio.gather` alone does not force overlap: two uploads can run start-to-finish in strict
    sequence and still satisfy a cap assertion, which pins the aggregate arithmetic rather than
    the mutual exclusion. This holds ``LockScope.PROJECT`` for ``proj`` from a *gate* connection,
    starts both uploads, waits until **both** backends are queued on that lock, and only then
    releases it — so each upload's reservation is known to have contended for the lock the
    invariant depends on.
    """
    gate = await _connect_pooled_shape(url)
    try:
        tasks = [asyncio.create_task(factory()) for factory in coro_factories]
        async with gate.transaction(), advisory_xact_lock(gate, LockScope.PROJECT, "proj"):
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if await _ungranted_advisory_locks(url) >= len(tasks):
                    break
                await asyncio.sleep(0.02)
            else:  # pragma: no cover - a hang here is a real defect, not a flake
                raise AssertionError("uploads never queued on the PROJECT lock")
        # Gate released: the uploads now serialize against each other, not against the gate.
        return list(await asyncio.gather(*tasks))
    finally:
        await gate.close()


def _quarantine(payload: bytes, key: str = "uploads/q/proj/rootfs.qcow2") -> _FakeStore:
    return _FakeStore({key: payload})


async def _register(
    conn: psycopg.AsyncConnection,
    store: _FakeStore,
    *,
    project: str = "proj",
    principal: str = "alice",
    name: str = "myrootfs",
    arch: str = "x86_64",
    quarantine_key: str = "uploads/q/proj/rootfs.qcow2",
    expires_at: datetime | None = None,
    inspect: InspectSeam | None = None,
):
    return await register_private_upload(
        conn,
        store,
        request=PrivateUploadRequest(
            project=project,
            principal=principal,
            name=name,
            provider="local-libvirt",
            arch=arch,
            quarantine_key=quarantine_key,
            expires_at=expires_at or (_DT + timedelta(days=3)),
            required=_REQUIRED,
        ),
        inspect=inspect or _conforming(),
    )


def _publish_request(*, name: str = "myrootfs", payload: bytes = b"unused"):
    """A private ``PublishRequest`` for the project, for tests driving the publish steps."""
    from kdive.services.images.publish import PublishRequest

    return PublishRequest(
        provider="local-libvirt",
        name=name,
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        capabilities=(),
        provenance={},
        visibility=ImageVisibility.PRIVATE,
        owner="proj",
        expires_at=_DT + timedelta(days=3),
    )


async def _row_state(conn: psycopg.AsyncConnection, row_id) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT state FROM image_catalog WHERE id = %s", (row_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


_UPLOAD_TOOL = "images.upload"


async def _denial_rows(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log WHERE tool = %s AND transition = 'denied'",
            (_UPLOAD_TOOL,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_clamp_expiry_caps_at_now_plus_lifetime_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_LIFETIME_MAX.name, "3600")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ceiling = now + timedelta(seconds=3600)
    # A far-future request is clamped down to the ceiling (now + max), never a past instant.
    assert _clamp_expiry(now + timedelta(days=365), now=now) == ceiling
    # A within-ceiling request passes through unchanged.
    earlier = now + timedelta(minutes=10)
    assert _clamp_expiry(earlier, now=now) == earlier


def test_quota_denial_admits_within_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "2")
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "100")
    # One more row still fits the count cap and the bytes exactly reach (not exceed) the cap.
    assert _quota_denial(project="proj", count=1, used_bytes=40, new_bytes=60) is None


def test_quota_denial_count_cap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "1000000")
    denial = _quota_denial(project="proj", count=1, used_bytes=0, new_bytes=0)
    assert denial is not None
    assert denial.category is ErrorCategory.QUOTA_EXCEEDED
    assert str(denial) == "project 'proj' is at its private-image count cap"
    assert denial.details == {"used": 1, "cap": 1}


def test_quota_denial_bytes_cap_error_and_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "100")
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "10")
    # Exactly at the cap is admitted (half-open: only strictly-over denies).
    assert _quota_denial(project="proj", count=0, used_bytes=4, new_bytes=6) is None
    denial = _quota_denial(project="proj", count=0, used_bytes=5, new_bytes=6)
    assert denial is not None
    assert denial.category is ErrorCategory.QUOTA_EXCEEDED
    assert str(denial) == "project 'proj' would exceed its private-image bytes cap"
    assert denial.details == {"used_bytes": 5, "new_bytes": 6, "cap_bytes": 10}


def test_reject_oversize_upload_rejects_and_respects_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "10")

    async def _run() -> None:
        oversize = _FakeStore({"q/big": b"this-is-more-than-ten"})
        with pytest.raises(CategorizedError) as err:
            await _reject_oversize_upload(oversize, "q/big")
        assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
        assert str(err.value) == "uploaded image exceeds the per-project private-image bytes cap"
        assert err.value.details == {"size_bytes": 21, "cap_bytes": 10}
        # An object exactly at the cap is admitted (strictly-over rejects).
        at_cap = _FakeStore({"q/exact": b"0123456789"})
        await _reject_oversize_upload(at_cap, "q/exact")
        # A vanished quarantined object is a STALE_HANDLE, not a quota denial.
        with pytest.raises(CategorizedError) as gone:
            await _reject_oversize_upload(_FakeStore(), "q/missing")
        assert gone.value.category is ErrorCategory.STALE_HANDLE

    asyncio.run(_run())


def test_project_usage_counts_rows_and_sums_reserved_bytes(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "10")
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "1000000")
    payload_a = b"rootfs-aaaa"
    payload_b = b"rootfs-bbbbbbbbbbbb"
    store = _quarantine(payload_a, key="uploads/q/proj/a.qcow2")
    store._objects["uploads/q/proj/b.qcow2"] = payload_b  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _register(conn, store, name="img-a", quarantine_key="uploads/q/proj/a.qcow2")
            await _register(conn, store, name="img-b", quarantine_key="uploads/q/proj/b.qcow2")
            count, total = await _project_usage(conn, "proj", adopting=None)
            # Another project's rows are not this project's usage.
            assert await _project_usage(conn, "other", adopting=None) == (0, 0)
        # Two live private rows, and the byte total is the sum of both rows' recorded sizes (not a
        # last-wins overwrite and not an off-by-one initial accumulator).
        assert count == 2
        assert total == len(payload_a) + len(payload_b)

    asyncio.run(_run())


def test_reserved_pending_row_occupies_quota_before_its_object_exists(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The invariant the shortened lock span rests on: the `pending` row committed under the lock
    # already carries its bytes, so a concurrent reader sees the claim even though the object has
    # not been written. Before ADR-0520 the row's bytes came from a HEAD of an object that does
    # not yet exist, so a pending row contributed 0 and could not serve as a reservation.
    from kdive.services.images.publish import reserve_publish

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "10")
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "1000000")
    store = _quarantine(b"unused")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            reservation = await reserve_publish(
                conn, _publish_request(name="reserved"), size_bytes=4096, principal="alice"
            )
            # Nothing was written to the store, and the row is `pending`, not `registered`.
            assert store.puts == []
            assert await _row_state(conn, reservation.row_id) == ImageState.PENDING.value
            assert await _project_usage(conn, "proj", adopting=None) == (1, 4096)

    asyncio.run(_run())


def test_registers_private_image_resolving_only_within_owning_project(migrated_url: str) -> None:
    store = _quarantine(b"conforming-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await _register(conn, store)
            assert entry.state is ImageState.REGISTERED
            assert entry.visibility is ImageVisibility.PRIVATE
            assert entry.owner == "proj"
            assert entry.object_key is not None
            assert entry.publication_attempt_id is None
            assert entry.publication_principal is None
            # Resolves for the owning project, not for another.
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="proj")
            assert mine is not None and mine.id == entry.id
            assert await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other") is None

    asyncio.run(_run())


def test_registered_private_name_reupload_conflicts_before_publish(migrated_url: str) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    payload = b"first-rootfs"
    replacement = b"replacement-rootfs"
    store = _quarantine(payload)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await _register(conn, store)
            puts_after_first = list(store.puts)
            store._objects["uploads/q/proj/replacement.qcow2"] = replacement  # noqa: SLF001 - test seam

            with pytest.raises(CategorizedError) as err:
                await _register(
                    conn,
                    store,
                    name="myrootfs",
                    quarantine_key="uploads/q/proj/replacement.qcow2",
                )
            assert err.value.category is ErrorCategory.CONFLICT
            assert "images.delete" in str(err.value)
            assert "images.upload" in str(err.value)
            assert store.puts == puts_after_first

            still_registered = await IMAGE_CATALOG.get(conn, first.id)
            assert still_registered == first
            assert first.object_key is not None
            registered_bytes = store._objects[first.object_key]  # noqa: SLF001 - integrity test seam
            assert "sha256:" + hashlib.sha256(registered_bytes).hexdigest() == first.digest
            assert len(await IMAGE_CATALOG.list_all(conn)) == 1

    asyncio.run(_run())


def test_registered_private_name_conflict_excludes_architecture(migrated_url: str) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _quarantine(b"x86-rootfs", key="uploads/q/proj/x86.qcow2")
    store._objects["uploads/q/proj/arm.qcow2"] = b"arm-rootfs"  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await _register(
                conn,
                store,
                name="shared-name",
                arch="x86_64",
                quarantine_key="uploads/q/proj/x86.qcow2",
            )
            puts_after_first = list(store.puts)

            with pytest.raises(RegisteredPrivateNameConflict) as err:
                await _register(
                    conn,
                    store,
                    name="shared-name",
                    arch="aarch64",
                    quarantine_key="uploads/q/proj/arm.qcow2",
                )
            assert err.value.category is ErrorCategory.CONFLICT
            assert store.puts == puts_after_first
            assert await IMAGE_CATALOG.get(conn, first.id) == first
            assert len(await IMAGE_CATALOG.list_all(conn)) == 1

    asyncio.run(_run())


def test_registered_private_name_conflict_is_owner_scoped(migrated_url: str) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _quarantine(b"project-rootfs", key="uploads/q/proj/shared.qcow2")
    store._objects["uploads/q/other/shared.qcow2"] = b"other-rootfs"  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            project_entry = await _register(
                conn, store, name="shared", quarantine_key="uploads/q/proj/shared.qcow2"
            )
            other_entry = await _register(
                conn,
                store,
                project="other",
                name="shared",
                quarantine_key="uploads/q/other/shared.qcow2",
            )
            assert project_entry.owner == "proj"
            assert other_entry.owner == "other"
            assert len(await IMAGE_CATALOG.list_all(conn)) == 2

    asyncio.run(_run())


def test_private_shadows_public_on_same_provider_name(migrated_url: str) -> None:
    from kdive.services.images.publish import PublishRequest, publish_image

    payload = b"private-rootfs"
    store = _quarantine(payload)

    async def _run(tmp: Path) -> None:
        async with await _connect(migrated_url) as conn:
            pub_src = tmp / "pub.qcow2"
            pub_src.write_bytes(b"public-rootfs")
            await publish_image(
                conn,
                store,
                request=PublishRequest(
                    provider="local-libvirt",
                    name="myrootfs",
                    arch="x86_64",
                    format="qcow2",
                    root_device="/dev/vda",
                    digest="sha256:" + hashlib.sha256(b"public-rootfs").hexdigest(),
                    capabilities=(),
                    provenance={},
                    visibility=ImageVisibility.PUBLIC,
                ),
                source=pub_src,
            )
            private = await _register(conn, store)
            # The owning project gets its private image; another project gets the public one.
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="proj")
            other = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other")
            assert mine is not None and mine.id == private.id
            assert other is not None and other.visibility is ImageVisibility.PUBLIC

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(_run(Path(d)))


def test_non_conforming_image_rejected_while_quarantined(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _quarantine(b"missing-drgn-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, inspect=_missing("drgn"))
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert "drgn" in str(err.value)
            assert err.value.details.get("missing") == "drgn"
            # Never registered: no catalog row, the object never left quarantine (no put).
            assert await IMAGE_CATALOG.list_all(conn) == []
            assert store.puts == []

    asyncio.run(_run())


def test_over_count_cap_denied_fail_closed_and_audited(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")
    store = _quarantine(b"rootfs-a")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _register(conn, store, name="first")
            denied_before = await _denial_rows(conn)
            store._objects["uploads/q/proj/b.qcow2"] = b"rootfs-b"  # noqa: SLF001 - test seam
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            assert str(err.value) == "project 'proj' is at its private-image count cap"
            # Fail-closed: the second image is not registered, and the denial is audited.
            registered = [r for r in await IMAGE_CATALOG.list_all(conn) if r.name == "second"]
            assert registered == []
            assert await _denial_rows(conn) == denied_before + 1
            # The audit row carries the human-readable reason and the pinned args digest.
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT reason, args_digest FROM audit_log "
                    "WHERE tool = %s AND transition = 'denied' ORDER BY ts DESC LIMIT 1",
                    (_UPLOAD_TOOL,),
                )
                audit_row = await cur.fetchone()
            assert audit_row is not None
            assert audit_row[0] == "project 'proj' is at its private-image count cap"
            assert audit_row[1] == args_digest(
                {"provider": "local-libvirt", "name": "second", "visibility": "private"}
            )

    asyncio.run(_run())


def test_over_bytes_cap_denied_fail_closed(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "10")
    store = _quarantine(b"this-is-more-than-ten-bytes")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store)
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            assert store.puts == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("field", "label"),
    [("provider", "provider"), ("name", "name"), ("arch", "arch"), ("project", "owner")],
)
def test_traversal_bearing_identity_component_rejected_before_staging(
    migrated_url: str, field: str, label: str
) -> None:
    # A `/`-bearing identity component must be rejected up front (it would otherwise fold into the
    # staged temp path / object key); the object is never read or written, and the error names the
    # offending component.
    store = _quarantine(b"rootfs")
    fields: dict[str, object] = {
        "project": "proj",
        "principal": "alice",
        "name": "myrootfs",
        "provider": "local-libvirt",
        "arch": "x86_64",
        "quarantine_key": "uploads/q/proj/rootfs.qcow2",
        "expires_at": _DT + timedelta(days=3),
        "required": _REQUIRED,
    }
    fields[field] = "../../etc/evil"

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await register_private_upload(
                    conn,
                    store,
                    request=PrivateUploadRequest(**fields),  # ty: ignore[invalid-argument-type]
                    inspect=_conforming(),
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert f"{label!r}" in str(err.value)  # the rejection names the offending component
            assert store.puts == []

    asyncio.run(_run())


def test_accumulated_bytes_cap_denied_under_lock(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither image alone exceeds the cap, but the second pushes the project total over it. The
    # under-lock authoritative check (current usage + new bytes) must deny — not just the
    # single-object pre-check.
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "20")
    store = _quarantine(b"twelve-bytes", key="uploads/q/proj/a.qcow2")  # 12 bytes
    store._objects["uploads/q/proj/b.qcow2"] = b"twelve-bytes"  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _register(conn, store, name="first", quarantine_key="uploads/q/proj/a.qcow2")
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED

    asyncio.run(_run())


def test_concurrent_uploads_cannot_both_pass_the_cap(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")

    async def _run() -> None:
        store_a = _quarantine(b"rootfs-aaaa", key="uploads/q/proj/a.qcow2")
        store_b = _quarantine(b"rootfs-bbbb", key="uploads/q/proj/b.qcow2")
        # Share one object namespace so each sees the other's registered image.
        store_b._objects.update(store_a._objects)  # noqa: SLF001 - test seam
        store_a._objects.update(store_b._objects)  # noqa: SLF001 - test seam

        async def _one(store: _FakeStore, name: str, key: str) -> object:
            conn = await _connect(migrated_url)
            try:
                return await _register(conn, store, name=name, quarantine_key=key)
            except CategorizedError as exc:
                return exc
            finally:
                await conn.close()

        results = await _run_both_contending(
            migrated_url,
            lambda: _one(store_a, "alpha", "uploads/q/proj/a.qcow2"),
            lambda: _one(store_b, "beta", "uploads/q/proj/b.qcow2"),
        )
        denials = [r for r in results if isinstance(r, CategorizedError)]
        assert len(denials) == 1
        assert denials[0].category is ErrorCategory.QUOTA_EXCEEDED

        async with await _connect(migrated_url) as conn:
            registered = [
                r for r in await IMAGE_CATALOG.list_all(conn) if r.state is ImageState.REGISTERED
            ]
            assert len(registered) == 1

    asyncio.run(_run())


def test_advisory_lock_probe_reports_a_held_lock(migrated_url: str) -> None:
    # Positive control for `_advisory_locks_held_by`. Every other use of it asserts `== 0`, so a
    # helper broken in any direction — wrong pid, wrong locktype filter, a query that can only
    # return 0 — would make those assertions pass vacuously, and one of them is the property this
    # whole change exists to establish. This is the only test that pins a non-zero reading.
    async def _run() -> None:
        async with await _connect_pooled_shape(migrated_url) as conn:
            assert await _advisory_locks_held_by(migrated_url, conn.info.backend_pid) == 0
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, "proj"):
                assert await _advisory_locks_held_by(migrated_url, conn.info.backend_pid) >= 1
            # And it drops back once the holding transaction ends, so it tracks the lock rather
            # than merely counting something that grows.
            assert await _advisory_locks_held_by(migrated_url, conn.info.backend_pid) == 0

    asyncio.run(_run())


def test_concurrent_same_identity_uploads_cannot_both_register(migrated_url: str) -> None:
    # Two uploads of the *same* provider/name/arch to one project, forced to overlap. The second
    # to reserve adopts the first's `pending` row (ADR-0092 idempotency) and overwrites its
    # `digest` and attempt-specific object key. Registering on `id` alone would let both flip that
    # one row to `registered` and return success. The publication fence serializes their writes,
    # and its revalidation makes the superseded attempt fail with `CONFLICT` before its write.
    from kdive.db.repositories import IMAGE_CATALOG

    first_put = True
    put_order_lock = threading.Lock()
    publishing_loop: list[asyncio.AbstractEventLoop] = []

    class _ContendedStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            nonlocal first_put
            if request.key().endswith(".qcow2"):
                with put_order_lock:
                    wait_for_adoption = first_put
                    first_put = False
                if wait_for_adoption:
                    asyncio.run_coroutine_threadsafe(
                        _wait_for_pending_image_lock_waiter(migrated_url), publishing_loop[0]
                    ).result(timeout=10)
            return super().put_artifact(request)

    store = _ContendedStore(
        {
            "uploads/q/proj/a.qcow2": b"alpha-bytes",
            "uploads/q/proj/b.qcow2": b"beta-bytes-differ",
        }
    )

    async def _run() -> None:
        publishing_loop.append(asyncio.get_running_loop())

        def _one(store: _FakeStore, key: str):
            async def _go() -> object:
                conn = await _connect(migrated_url)
                try:
                    # Same `name` for both — that is the collision under test.
                    return await _register(conn, store, name="shared", quarantine_key=key)
                except CategorizedError as exc:
                    return exc
                finally:
                    await conn.close()

            return _go

        results = await _run_both_contending(
            migrated_url,
            _one(store, "uploads/q/proj/a.qcow2"),
            _one(store, "uploads/q/proj/b.qcow2"),
        )
        conflicts = [r for r in results if isinstance(r, CategorizedError)]
        registered = [r for r in results if not isinstance(r, CategorizedError)]
        # Exactly one attempt owns the row it registers; the superseded one is told so.
        assert len(conflicts) == 1
        assert conflicts[0].category is ErrorCategory.CONFLICT
        assert "superseded" in str(conflicts[0])
        assert len(registered) == 1

        async with await _connect(migrated_url) as conn:
            rows = await IMAGE_CATALOG.list_all(conn)
            # One adopted row, registered once — not two rows and not a second `pending` leak.
            assert len(rows) == 1
            assert rows[0].state is ImageState.REGISTERED
            assert rows[0].id == registered[0].id  # ty: ignore[unresolved-attribute]
            assert rows[0].object_key is not None
            registered_bytes = store._objects[rows[0].object_key]  # noqa: SLF001 - integrity test seam
            assert "sha256:" + hashlib.sha256(registered_bytes).hexdigest() == rows[0].digest

    asyncio.run(_run())


def test_registration_is_ordered_after_an_inflight_duplicate_reservation(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG
    from kdive.domain.catalog.images import ImageCatalogEntry
    from kdive.services.images import upload as image_upload
    from kdive.services.images.publish import PublishRequest, PublishReservation

    first_put_started = threading.Event()
    release_first_put = threading.Event()
    store = _FirstPutGateStore(
        {
            "uploads/q/proj/a.qcow2": b"alpha-bytes",
            "uploads/q/proj/b.qcow2": b"beta-bytes-differ",
        },
        first_put_started=first_put_started,
        release_first_put=release_first_put,
    )
    real_reserve = image_upload.reserve_publish
    reserve_calls = 0
    second_before_reserve: asyncio.Event
    release_second_reserve: asyncio.Event

    async def _reserve_with_forced_gap(
        conn: psycopg.AsyncConnection,
        request: PublishRequest,
        *,
        size_bytes: int,
        principal: str | None = None,
    ) -> PublishReservation:
        nonlocal reserve_calls
        reserve_calls += 1
        if reserve_calls == 2:
            # `_reserve_under_quota` already queried and saw only the first attempt's pending row;
            # pause before the adopt/insert statement while it still holds PROJECT.
            second_before_reserve.set()
            await release_second_reserve.wait()
        return await real_reserve(
            conn,
            request,
            size_bytes=size_bytes,
            principal=principal,
        )

    monkeypatch.setattr(image_upload, "reserve_publish", _reserve_with_forced_gap)

    async def _run() -> None:
        nonlocal second_before_reserve, release_second_reserve
        second_before_reserve = asyncio.Event()
        release_second_reserve = asyncio.Event()

        async def _one(key: str) -> object:
            conn = await _connect(migrated_url)
            try:
                return await _register(conn, store, name="shared", quarantine_key=key)
            except CategorizedError as exc:
                return exc
            finally:
                await conn.close()

        first = asyncio.create_task(_one("uploads/q/proj/a.qcow2"))
        assert await asyncio.to_thread(first_put_started.wait, 10)
        second = asyncio.create_task(_one("uploads/q/proj/b.qcow2"))
        await asyncio.wait_for(second_before_reserve.wait(), timeout=10)

        # The second upload now owns PROJECT in the exact gap under review. Let the first PUT
        # return and wait until its finish either registers (old ordering) or queues on PROJECT
        # (the required ordering), then allow the second reservation statement to run.
        release_first_put.set()
        finish_order = await _wait_for_registered_row_or_project_waiter(migrated_url)
        release_second_reserve.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert finish_order == "project_waiter"
        assert all(
            isinstance(result, (ImageCatalogEntry, CategorizedError)) for result in results
        ), results
        conflicts = [result for result in results if isinstance(result, CategorizedError)]
        registered = [result for result in results if isinstance(result, ImageCatalogEntry)]
        assert len(conflicts) == 1
        assert conflicts[0].category is ErrorCategory.CONFLICT
        assert "superseded" in str(conflicts[0])
        assert len(registered) == 1

        async with await _connect(migrated_url) as conn:
            rows = await IMAGE_CATALOG.list_all(conn)
            usage = await _project_usage(conn, "proj", adopting=None)
        assert len(rows) == 1
        assert rows[0] == registered[0]
        assert rows[0].object_key is not None
        registered_bytes = store._objects[rows[0].object_key]  # noqa: SLF001 - integrity seam
        assert "sha256:" + hashlib.sha256(registered_bytes).hexdigest() == rows[0].digest
        assert rows[0].size_bytes == len(registered_bytes)
        assert usage == (1, len(registered_bytes))
        # Each attempt wrote only its own attempt-specific key; the loser never overwrote the
        # winner, and its rowless key remains owned by the existing leaked-object recovery.
        assert len(store.puts) == 2
        assert len(set(store.puts)) == 2

    asyncio.run(_run())


def test_only_image_publish_lock_is_held_while_the_object_is_written(migrated_url: str) -> None:
    # The property #1726 exists for, asserted at the only moment it can be observed: inside the
    # PUT. The store probes pg_locks from a *second* connection filtered to the publishing
    # backend's pid (probing the connection under test would itself open a transaction, ADR-0506)
    # and records what it saw. Before ADR-0520 the PROJECT lock was held here; after it, the
    # reservation transaction has committed and released it.
    project_lock_observed: list[bool] = []
    image_lock_observed: list[bool] = []
    publishing_pid: list[int] = []
    publishing_loop: list[asyncio.AbstractEventLoop] = []

    class _ProbingStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            project_lock_observed.append(
                asyncio.run_coroutine_threadsafe(
                    _scoped_lock_held_by(
                        migrated_url,
                        publishing_pid[0],
                        LockScope.PROJECT,
                        "proj",
                    ),
                    publishing_loop[0],
                ).result()
            )
            image_lock_observed.append(
                asyncio.run_coroutine_threadsafe(
                    _pending_image_lock_held_by(migrated_url, publishing_pid[0]),
                    publishing_loop[0],
                ).result()
            )
            return super().put_artifact(request)

    store = _ProbingStore({"uploads/q/proj/rootfs.qcow2": b"conforming-rootfs"})

    async def _run() -> None:
        publishing_loop.append(asyncio.get_running_loop())
        async with await _connect(migrated_url) as conn:
            publishing_pid.append(conn.info.backend_pid)
            entry = await _register(conn, store)
            assert entry.state is ImageState.REGISTERED
        # The PUT happened under its row fence, but never under the project quota lock.
        assert store.puts != []
        assert project_lock_observed == [False]
        assert image_lock_observed == [True]

    asyncio.run(_run())


def test_concurrent_uploads_cannot_both_pass_the_bytes_cap(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bytes half of the fail-closed invariant, which is the half that now rests on the
    # reservation's recorded `size_bytes` rather than on a HEAD of an object written under the
    # lock. Each image is 12 bytes and the cap admits 20, so exactly one may pass.
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "20")

    async def _run() -> None:
        store_a = _quarantine(b"twelve-bytes", key="uploads/q/proj/a.qcow2")
        store_b = _quarantine(b"twelve-bytes", key="uploads/q/proj/b.qcow2")
        # Share one object namespace so each sees the other's published image.
        store_b._objects.update(store_a._objects)  # noqa: SLF001 - test seam
        store_a._objects.update(store_b._objects)  # noqa: SLF001 - test seam

        async def _one(store: _FakeStore, name: str, key: str) -> object:
            conn = await _connect(migrated_url)
            try:
                return await _register(conn, store, name=name, quarantine_key=key)
            except CategorizedError as exc:
                return exc
            finally:
                await conn.close()

        results = await _run_both_contending(
            migrated_url,
            lambda: _one(store_a, "alpha", "uploads/q/proj/a.qcow2"),
            lambda: _one(store_b, "beta", "uploads/q/proj/b.qcow2"),
        )
        denials = [r for r in results if isinstance(r, CategorizedError)]
        assert len(denials) == 1
        assert denials[0].category is ErrorCategory.QUOTA_EXCEEDED
        assert str(denials[0]) == "project 'proj' would exceed its private-image bytes cap"

        async with await _connect(migrated_url) as conn:
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len([r for r in rows if r.state is ImageState.REGISTERED]) == 1
            # The one that passed recorded its real size, so the next reader sees 12, not 0.
            assert await _project_usage(conn, "proj", adopting=None) == (1, 12)

    asyncio.run(_run())


def test_a_failed_put_leaves_a_reservation_the_dangling_sweep_reclaims(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The worker-dies-mid-upload path, which is the cost of committing the reservation before the
    # write. The reservation must (a) still hold quota — otherwise the cap is fail-open between
    # reserve and write — and (b) not hold it forever. Reclamation is the existing ADR-0092
    # dangling sweep on `pending_since + grace`, not a bespoke rollback (ADR-0520 §4).
    from kdive.reconciler.cleanup.images import repair_dangling_images

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "20")
    caplog.set_level(logging.WARNING, logger="kdive.services.images.upload")

    class _DyingStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            raise CategorizedError(
                "object store is unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={},
            )

    dying = _DyingStore({"uploads/q/proj/a.qcow2": b"twelve-bytes"})

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, dying, name="doomed", quarantine_key="uploads/q/proj/a.qcow2")
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            # The abandonment is announced. Without it the project silently carries the bytes
            # until the sweep runs, and an operator chasing the resulting QUOTA_EXCEEDED has no
            # trail; the error the caller sees names the store, not the quota it just stranded.
            abandoned = [
                r
                for r in caplog.records
                if r.levelno >= logging.WARNING and "abandoned" in r.getMessage()
            ]
            assert len(abandoned) == 1
            assert "proj" in abandoned[0].getMessage()
            assert "12 byte(s)" in abandoned[0].getMessage()

            # (a) The reservation survived the failed write and still occupies its bytes, so a
            # second upload that would jointly breach the cap is denied rather than admitted.
            assert await _project_usage(conn, "proj", adopting=None) == (1, 12)
            healthy = _quarantine(b"twelve-bytes", key="uploads/q/proj/b.qcow2")
            with pytest.raises(CategorizedError) as blocked:
                await _register(conn, healthy, name="next", quarantine_key="uploads/q/proj/b.qcow2")
            assert blocked.value.category is ErrorCategory.QUOTA_EXCEEDED

            # (b) Past its publish deadline the reconciler removes the row — object missing, grace
            # elapsed — and the project's quota is released.
            removed = await repair_dangling_images(conn, _SweepStore(), timedelta(seconds=0))
            assert removed == 1
            assert await _project_usage(conn, "proj", adopting=None) == (0, 0)
            # With the quota released the previously-blocked upload now succeeds.
            entry = await _register(
                conn, healthy, name="next", quarantine_key="uploads/q/proj/b.qcow2"
            )
            assert entry.state is ImageState.REGISTERED

    asyncio.run(_run())


def test_retrying_an_abandoned_reservation_is_not_double_counted(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The retry ADR-0520 §4 names as the recovery from a failed write must not be denied by the
    # bytes its own abandoned reservation is holding. `reserve_publish` *adopts* that row and
    # overwrites its size rather than adding a second one, so counting it in the usage read would
    # charge the project twice for one image and lock the user out for the whole publish grace.
    # The cap here admits one 12-byte image and not two; the earlier test misses this because it
    # leaves the cap at its 50 GiB default.
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "20")

    class _DyingStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            raise CategorizedError(
                "object store is unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={},
            )

    first = _DyingStore({"uploads/q/proj/a.qcow2": b"twelve-bytes"})
    retry = _quarantine(b"twelve-bytes", key="uploads/q/proj/b.qcow2")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, first, quarantine_key="uploads/q/proj/a.qcow2")
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            # The abandoned reservation is holding the full cap against everyone...
            assert await _project_usage(conn, "proj", adopting=None) == (1, 12)
            # ...but not against the retry of that same image, which will adopt it.
            entry = await _register(conn, retry, quarantine_key="uploads/q/proj/b.qcow2")
            assert entry.state is ImageState.REGISTERED
            assert await _project_usage(conn, "proj", adopting=None) == (1, 12)

    asyncio.run(_run())


def test_retrying_an_abandoned_reservation_re_reserves_the_new_size(migrated_url: str) -> None:
    # Publish adopts this identity's in-flight `pending` row rather than colliding with it
    # (ADR-0092), so a retry after a failed write reuses the abandoned reservation. The adopted
    # row must re-reserve *this* attempt's size: leaving the previous attempt's bytes on it makes
    # the project's usage a lie in whichever direction the two sizes differ.
    class _DyingStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            raise CategorizedError(
                "object store is unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={},
            )

    first = _DyingStore({"uploads/q/proj/a.qcow2": b"twelve-bytes"})
    retry = _quarantine(b"four", key="uploads/q/proj/b.qcow2")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError):
                await _register(conn, first, quarantine_key="uploads/q/proj/a.qcow2")
            assert await _project_usage(conn, "proj", adopting=None) == (1, 12)
            # Same identity (provider/name/arch/visibility/owner), smaller image.
            entry = await _register(conn, retry, quarantine_key="uploads/q/proj/b.qcow2")
            assert entry.state is ImageState.REGISTERED
            # One row still — adopted, not duplicated — carrying the retry's size, not the
            # abandoned attempt's.
            assert await _project_usage(conn, "proj", adopting=None) == (1, 4)
            # And the retry's digest. The adopt used to leave the abandoned attempt's digest on
            # the row while writing the retry's bytes, which registers an image whose object can
            # never satisfy the materialization fetch's `sha256(object) == row.digest` gate.
            written = retry._objects[entry.object_key]  # noqa: SLF001 - test seam
            assert entry.digest == "sha256:" + hashlib.sha256(written).hexdigest()

    asyncio.run(_run())


class _SweepStore:
    """The ``ImageSweepStore`` shape for the dangling sweep: every image object is absent."""

    def list_image_objects(self) -> list[artifact_types.ObjectListing]:
        return []

    def head_present(self, key: str) -> bool:
        return False

    def head(self, key: str) -> artifact_types.HeadResult | None:
        return None

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        assert limit == 20
        raise AssertionError(f"the dangling sweep must not delete objects (got {key!r})")

    def delete_version(self, key: str, version_id: str) -> None:
        raise AssertionError(f"the dangling sweep must not delete objects (got {key!r})")

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        raise AssertionError("the dangling sweep must not write objects")


def test_publish_refuses_a_connection_that_already_opened_a_transaction(migrated_url: str) -> None:
    # The reservation must really commit and the PROJECT lock must really release, both at the
    # end of the locked block (ADR-0520). That only works if its `conn.transaction()` is a real
    # one: on a non-autocommit connection with a statement already run, it would be a SAVEPOINT,
    # whose release commits nothing and releases no advisory lock. The reservation would then be
    # invisible to a concurrent upload — the cap goes fail-open — *and* the lock would be held to
    # the caller's commit, right back across the PUT this change moved it off (ADR-0506,
    # ADR-0516 §1). No caller does this today, which is exactly why it is guarded rather than
    # left to hold by luck; the dirty case is constructed here deliberately.
    from kdive.db.repositories import IMAGE_CATALOG

    store = _quarantine(b"conforming-rootfs")

    async def _run() -> None:
        async with await _connect_pooled_shape(migrated_url) as conn:
            await conn.execute("SELECT 1")  # one bare read is all it takes
            assert conn.info.transaction_status is TransactionStatus.INTRANS
            with pytest.raises(RuntimeError, match="needs a transaction-free connection"):
                await _register(conn, store)
            await conn.rollback()
            # Refused *before* the publish: no object written, no catalog row.
            assert store.puts == []
            assert await IMAGE_CATALOG.list_all(conn) == []

    asyncio.run(_run())


def test_publish_accepts_a_clean_pooled_connection_and_releases_the_lock(migrated_url: str) -> None:
    # The live MCP shape: `_register_upload` takes a fresh `pool.connection()` (non-autocommit)
    # and runs nothing on it before calling in. The guard must not fire there — a check that
    # rejects every legitimate call is worse than none — and the PROJECT lock must be gone once
    # the publish returns, which is the property the guard exists to keep true.
    store = _quarantine(b"conforming-rootfs")

    from kdive.db.repositories import IMAGE_CATALOG

    async def _run() -> None:
        async with await _connect_pooled_shape(migrated_url) as conn:
            entry = await _register(conn, store)
            assert entry.state is ImageState.REGISTERED
            assert store.puts != []
            # The publish's transaction really committed and really ended: the row is visible to
            # a *different* connection, and the lock it took is released. A savepoint would leave
            # the row invisible and the lock held.
            async with await _connect(migrated_url) as observer:
                assert [r.id for r in await IMAGE_CATALOG.list_all(observer)] == [entry.id]
            assert await _advisory_locks_held_by(migrated_url, conn.info.backend_pid) == 0

    asyncio.run(_run())


def test_quota_denial_is_audited_durably_on_a_pooled_connection(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The denial path opens a *second* `conn.transaction()` (`_audit_denial`) after the locked one
    # has closed. On the live MCP shape that connection is non-autocommit, so this pins that the
    # denial audit really commits there rather than deferring to the caller — the existing denial
    # tests read the row back on their own autocommit connection, which cannot tell the two apart
    # (ADR-0506, ADR-0516). The connection is left untouched before the call so the new
    # top-level-transaction guard does not fire first.
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")
    store = _quarantine(b"rootfs-a")
    store._objects["uploads/q/proj/b.qcow2"] = b"rootfs-b"  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _register(seed, store, name="first")
        async with await _connect_pooled_shape(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            # Both transactions ended, so nothing is left open to defer the audit to.
            assert conn.info.transaction_status is TransactionStatus.IDLE
        # Durable: the denial row is readable from a connection that never saw the upload.
        async with await _connect(migrated_url) as observer:
            assert await _denial_rows(observer) == 1

    asyncio.run(_run())


def test_expiry_clamped_to_lifetime_max(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_LIFETIME_MAX.name, str(3600))
    store = _quarantine(b"rootfs-x")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            far = datetime.now(UTC) + timedelta(days=365)
            entry = await _register(conn, store, expires_at=far)
            assert entry.expires_at is not None
            # Clamped to roughly now + 1h, well below the requested year.
            assert entry.expires_at < datetime.now(UTC) + timedelta(hours=2)

    asyncio.run(_run())


def test_records_principal_in_audit_owner_is_project(
    migrated_url: str,
) -> None:
    store = _quarantine(b"audited-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await _register(conn, store, principal="bob", project="proj")
            assert entry.owner == "proj"
            # The recorded provenance pins the uploading principal and source object.
            assert entry.provenance == {
                "upload": {
                    "principal": "bob",
                    "quarantine_key": "uploads/q/proj/rootfs.qcow2",
                }
            }
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, project, args_digest FROM audit_log "
                    "WHERE transition = %s ORDER BY ts DESC LIMIT 1",
                    ("private-upload:registered",),
                )
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "bob"
            assert row[1] == "proj"
            assert row[2] == args_digest(
                {"provider": entry.provider, "name": entry.name, "arch": entry.arch}
            )
            ownerless = entry.model_copy(update={"owner": None})
            with pytest.raises(RuntimeError, match="no owner project"):
                await record_private_registration(conn, ownerless, "bob")

    asyncio.run(_run())
