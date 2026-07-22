"""Project-private upload registration (ADR-0093, issue #286).

``register_private_upload`` runs under the project advisory lock: it enforces the per-project
count/bytes quota fail-closed, validates the quarantined object's guest contract, then delegates
to ``publish_image`` with ``visibility='private'``/``owner=project``. These tests pin: a
non-conforming image is rejected with a named reason while still quarantined (never registered);
an over-cap upload is denied fail-closed and audited; two concurrent uploads cannot both pass the
cap (held PROJECT lock); a registered private image resolves only within its owning project and
shadows a same-identity public image there.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from kdive.artifacts import storage as artifact_types
from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_BYTES,
    IMAGE_PRIVATE_MAX_COUNT,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.images import ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.catalog import resolve_rootfs
from kdive.images.cataloging.validation import GUEST_CONTRACT_PATHS, InspectSeam
from kdive.security.audit import args_digest
from kdive.services.images.upload import (
    PrivateUploadRequest,
    _clamp_expiry,
    _project_usage,
    _quota_denial,
    _reject_oversize_upload,
    register_private_upload,
)

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
        etag = hashlib.md5(request.data).hexdigest()  # noqa: S324 - etag stand-in, not security
        return artifact_types.StoredArtifact(
            key, etag, request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(size_bytes=len(data), checksum_sha256=None, etag="etag")


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _quarantine(payload: bytes, key: str = "uploads/q/proj/rootfs.qcow2") -> _FakeStore:
    return _FakeStore({key: payload})


async def _register(
    conn: psycopg.AsyncConnection,
    store: _FakeStore,
    *,
    project: str = "proj",
    principal: str = "alice",
    name: str = "myrootfs",
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
            arch="x86_64",
            quarantine_key=quarantine_key,
            expires_at=expires_at or (_DT + timedelta(days=3)),
            required=_REQUIRED,
        ),
        inspect=inspect or _conforming(),
    )


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


def test_project_usage_counts_rows_and_sums_object_bytes(
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
            count, total = await _project_usage(conn, "proj", store)
        # Two live private rows, and the byte total is the sum of both objects (not a last-wins
        # overwrite and not an off-by-one initial accumulator).
        assert count == 2
        assert total == len(payload_a) + len(payload_b)

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
            # Resolves for the owning project, not for another.
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="proj")
            assert mine is not None and mine.id == entry.id
            assert await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other") is None

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

        results = await asyncio.gather(
            _one(store_a, "alpha", "uploads/q/proj/a.qcow2"),
            _one(store_b, "beta", "uploads/q/proj/b.qcow2"),
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

    asyncio.run(_run())
