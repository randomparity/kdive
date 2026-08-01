"""Row-first publish/register two-write (ADR-0092, issue #285).

The service writes the ``pending`` row before the object, HEAD-gates, then flips to
``registered``. These tests pin: the success path (a ``registered`` row whose object HEADs
and resolves), crash-after-pending-before-object adoptability (no unique-violation wedge),
idempotent re-run (adopt the in-flight ``pending`` row, re-arm ``pending_since``), and realizing
a seeded ``defined`` baseline through the same path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from kdive.artifacts import storage as artifact_types
from kdive.db.locks import LockScope, _lock_key
from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.catalog.images import (
    Capability,
    ImageCatalogEntry,
    ImageState,
    ImageVisibility,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.catalog import resolve_rootfs
from kdive.services.images.publish import (
    PublishRequest,
    _write_config_best_effort,
    digest_sha256_b64,
    finish_publish,
    kernel_config_object_key,
    publish_image,
    reserve_publish,
    write_publish_object,
)
from tests.clock import STORE_MTIME

_QCOW2 = b"qcow2-bytes-for-publish-test"
_DIGEST = "sha256:" + hashlib.sha256(_QCOW2).hexdigest()
_CHECKSUM = base64.b64encode(hashlib.sha256(_QCOW2).digest()).decode("ascii")
_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    """An in-memory ObjectStore stand-in: put records bytes, head reflects them."""

    def __init__(
        self,
        *,
        fail_put: bool = False,
        drop_object: bool = False,
        fail_config: bool = False,
    ) -> None:
        self._objects: dict[str, bytes] = {}
        self._fail_put = fail_put
        self._drop_object = drop_object
        self._fail_config = fail_config
        self._checksums: dict[str, str | None] = {}
        self.puts: list[str] = []
        self.heads: list[str] = []

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        key = request.key()
        self.puts.append(key)
        if self._fail_put or (self._fail_config and key.endswith(".config")):
            raise CategorizedError(
                "object store unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            )
        if not self._drop_object:
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
        self.heads.append(key)
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(
            size_bytes=len(data),
            checksum_sha256=self._checksums[key],
            etag="etag",
            last_modified=STORE_MTIME,
            version_id="test-version",
        )


_PUBLIC_REQUEST = PublishRequest(
    provider="local-libvirt",
    name="base",
    arch="x86_64",
    format="qcow2",
    root_device="/dev/vda",
    digest=_DIGEST,
    capabilities=("agent", "kdump"),
    provenance={"releasever": "43"},
    visibility=ImageVisibility.PUBLIC,
)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


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


def _qcow2_source(tmp_path: Path) -> Path:
    src = tmp_path / "rootfs.qcow2"
    src.write_bytes(_QCOW2)
    return src


class _FixedHeadStore(_FakeStore):
    """A store that reports the requested HEAD result after accepting a qcow2 PUT."""

    def __init__(self, head_result: artifact_types.HeadResult | None) -> None:
        super().__init__()
        self._head_result = head_result

    def head(self, key: str) -> artifact_types.HeadResult | None:
        self.heads.append(key)
        return self._head_result


def test_digest_sha256_b64_converts_to_canonical_padded_base64() -> None:
    assert digest_sha256_b64(_DIGEST) == _CHECKSUM


@pytest.mark.parametrize(
    "digest",
    (
        "sha256:",
        "sha512:" + "0" * 64,
        "sha256:" + "g" * 64,
        "sha256:" + "0" * 62 + "  ",
        "SHA256:" + "0" * 64,
    ),
)
def test_digest_sha256_b64_rejects_non_sha256_hex_digest(digest: str) -> None:
    with pytest.raises(CategorizedError) as err:
        digest_sha256_b64(digest)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.value.details == {"digest": digest}


@pytest.mark.parametrize(
    ("head_result", "registered"),
    (
        (None, False),
        (
            artifact_types.HeadResult(
                size_bytes=len(_QCOW2) - 1,
                checksum_sha256=_CHECKSUM,
                etag="etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            ),
            False,
        ),
        (
            artifact_types.HeadResult(
                size_bytes=len(_QCOW2),
                checksum_sha256=None,
                etag="etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            ),
            False,
        ),
        (
            artifact_types.HeadResult(
                size_bytes=len(_QCOW2),
                checksum_sha256="not-valid-base64",
                etag="etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            ),
            False,
        ),
        (
            artifact_types.HeadResult(
                size_bytes=len(_QCOW2),
                checksum_sha256=base64.b64encode(
                    hashlib.sha256(b"a different qcow2").digest()
                ).decode("ascii"),
                etag="etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            ),
            False,
        ),
        (
            artifact_types.HeadResult(
                size_bytes=len(_QCOW2),
                checksum_sha256=_CHECKSUM,
                etag="etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            ),
            True,
        ),
    ),
)
def test_publish_registers_only_after_exact_size_and_checksum_head(
    migrated_url: str,
    tmp_path: Path,
    head_result: artifact_types.HeadResult | None,
    *,
    registered: bool,
) -> None:
    store = _FixedHeadStore(head_result)
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            if registered:
                entry = await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
                assert entry.state is ImageState.REGISTERED
            else:
                with pytest.raises(CategorizedError) as err:
                    await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
                assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
                row = (await IMAGE_CATALOG.list_all(conn))[0]
                assert row.state is ImageState.PENDING

    asyncio.run(_run())


def test_publish_rejects_malformed_digest_before_reservation(
    migrated_url: str, tmp_path: Path
) -> None:
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await publish_image(
                    conn,
                    _FakeStore(),
                    request=replace(_PUBLIC_REQUEST, digest="sha256:not-a-digest"),
                    source=source,
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert await IMAGE_CATALOG.list_all(conn) == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    "changed_field",
    ("row", "publication_attempt_id", "state", "object_key", "digest", "size_bytes"),
)
def test_publication_fence_revalidates_complete_reservation_before_write(
    migrated_url: str, changed_field: str
) -> None:
    async def _run() -> None:
        from kdive.services.images.publication_fence import publication_fence

        async with await _connect(migrated_url) as conn:
            reservation = await reserve_publish(conn, _PUBLIC_REQUEST, size_bytes=len(_QCOW2))
            if changed_field == "row":
                await conn.execute(
                    "UPDATE image_catalog SET publication_attempt_id = NULL, "
                    "publication_principal = NULL WHERE id = %s",
                    (reservation.row_id,),
                )
                await conn.execute("DELETE FROM image_catalog WHERE id = %s", (reservation.row_id,))
            elif changed_field == "state":
                await conn.execute(
                    "UPDATE image_catalog SET state = 'registered', "
                    "publication_attempt_id = NULL, publication_principal = NULL WHERE id = %s",
                    (reservation.row_id,),
                )
            elif changed_field == "publication_attempt_id":
                await conn.execute(
                    "UPDATE image_catalog SET publication_attempt_id = %s WHERE id = %s",
                    (uuid4(), reservation.row_id),
                )
            elif changed_field == "object_key":
                await conn.execute(
                    "UPDATE image_catalog SET object_key = %s WHERE id = %s",
                    (reservation.object_key + ".new", reservation.row_id),
                )
            elif changed_field == "digest":
                await conn.execute(
                    "UPDATE image_catalog SET digest = %s WHERE id = %s",
                    ("sha256:" + "0" * 64, reservation.row_id),
                )
            else:
                await conn.execute(
                    "UPDATE image_catalog SET size_bytes = size_bytes + 1 WHERE id = %s",
                    (reservation.row_id,),
                )

            reached_write = False
            with pytest.raises(CategorizedError) as err:
                async with publication_fence(conn, reservation):
                    reached_write = True
            assert err.value.category is ErrorCategory.CONFLICT
            assert not reached_write

    asyncio.run(_run())


@pytest.mark.parametrize("autocommit", (True, False), ids=("worker", "pooled"))
def test_publication_fence_enters_put_transaction_idle_and_holds_only_image_lock(
    migrated_url: str, tmp_path: Path, *, autocommit: bool
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingStore(_FakeStore):
        def put_artifact(
            self, request: artifact_types.ArtifactWriteRequest
        ) -> artifact_types.StoredArtifact:
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release blocked publication")
            return super().put_artifact(request)

    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        store = _BlockingStore()
        conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=autocommit)
        task = asyncio.create_task(
            publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            async with await _connect(migrated_url) as observer:
                row = (await IMAGE_CATALOG.list_all(observer))[0]
            assert conn.info.transaction_status is TransactionStatus.IDLE
            assert await _scoped_lock_held_by(
                migrated_url, conn.info.backend_pid, LockScope.IMAGE_PUBLISH, str(row.id)
            )
            assert not await _scoped_lock_held_by(
                migrated_url, conn.info.backend_pid, LockScope.PROJECT, "proj"
            )
        finally:
            release.set()
        entry = await task
        assert entry.state is ImageState.REGISTERED
        assert conn.autocommit is autocommit
        await conn.close()

    asyncio.run(_run())


def test_config_object_key_matches_kernel_config_object_key_public() -> None:
    """The plain-args helper produces the byte-identical key the request wrapper does (public)."""
    from kdive.services.images.publish import config_object_key

    assert config_object_key(
        _PUBLIC_REQUEST.provider,
        _PUBLIC_REQUEST.name,
        _PUBLIC_REQUEST.arch,
        _PUBLIC_REQUEST.visibility,
        _PUBLIC_REQUEST.owner,
    ) == kernel_config_object_key(_PUBLIC_REQUEST)


def test_config_object_key_matches_kernel_config_object_key_private() -> None:
    """Owner-scoped private key stays identical across the helper and the request wrapper."""
    from kdive.services.images.publish import config_object_key

    request = replace(
        _PUBLIC_REQUEST, visibility=ImageVisibility.PRIVATE, owner="proj", expires_at=_DT
    )
    key = config_object_key(
        request.provider, request.name, request.arch, request.visibility, request.owner
    )
    assert key == kernel_config_object_key(request)
    assert "local-libvirt__proj" in key


def test_publish_request_rejects_scope_fields_that_do_not_match_visibility() -> None:
    with pytest.raises(ValueError, match="owner must be set iff visibility is private"):
        replace(_PUBLIC_REQUEST, visibility=ImageVisibility.PRIVATE, expires_at=_DT)

    with pytest.raises(ValueError, match="expires_at must be set iff visibility is private"):
        replace(_PUBLIC_REQUEST, visibility=ImageVisibility.PRIVATE, owner="proj")

    with pytest.raises(ValueError, match="owner must be set iff visibility is private"):
        replace(_PUBLIC_REQUEST, owner="proj")


def test_publish_leaves_registered_row_that_heads_and_resolves(
    migrated_url: str, tmp_path: Path
) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
            assert entry.state is ImageState.REGISTERED
            assert entry.object_key is not None
            assert store.head(entry.object_key) is not None
            resolved = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert resolved is not None
            assert resolved.id == entry.id

    asyncio.run(_run())


def test_crash_after_pending_before_object_leaves_adoptable_state(
    migrated_url: str, tmp_path: Path
) -> None:
    # A store whose put fails models a crash after the pending row, before the object lands.
    failing = _FakeStore(fail_put=True)
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError):
                await publish_image(conn, failing, request=_PUBLIC_REQUEST, source=source)
            # The pending row survives, with an object_key set but no object behind it.
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len(rows) == 1
            assert rows[0].state is ImageState.PENDING
            assert rows[0].object_key is not None
            assert failing.head(rows[0].object_key) is None

            # A re-run adopts the pending row (no unique-violation wedge) and registers it.
            healthy = _FakeStore()
            entry = await publish_image(conn, healthy, request=_PUBLIC_REQUEST, source=source)
            assert entry.id == rows[0].id
            assert entry.state is ImageState.REGISTERED
            assert (await IMAGE_CATALOG.list_all(conn)) == [
                r for r in await IMAGE_CATALOG.list_all(conn) if r.id == entry.id
            ]

    asyncio.run(_run())


def test_rerun_adopts_pending_and_rearms_pending_since(migrated_url: str, tmp_path: Path) -> None:
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failing = _FakeStore(fail_put=True)
            with pytest.raises(CategorizedError):
                await publish_image(conn, failing, request=_PUBLIC_REQUEST, source=source)
            pending = (await IMAGE_CATALOG.list_all(conn))[0]
            original_since = pending.pending_since

            # Age the pending_since so a re-arm is observable.
            await conn.execute(
                "UPDATE image_catalog SET pending_since = %s WHERE id = %s",
                (original_since - timedelta(hours=2), pending.id),
            )

            healthy = _FakeStore()
            entry = await publish_image(conn, healthy, request=_PUBLIC_REQUEST, source=source)
            assert entry.id == pending.id
            assert entry.pending_since > original_since - timedelta(hours=2)

    asyncio.run(_run())


def test_each_adoption_mints_a_new_attempt_and_publish_keys(migrated_url: str) -> None:
    request = replace(_PUBLIC_REQUEST, kernel_config=_CONFIG)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await reserve_publish(conn, request, size_bytes=len(_QCOW2))
            second = await reserve_publish(conn, request, size_bytes=len(_QCOW2))
            assert first.row_id == second.row_id
            assert first.publication_attempt_id != second.publication_attempt_id
            assert first.object_key != second.object_key
            assert first.config_key != second.config_key

    asyncio.run(_run())


def test_private_adoption_replaces_persisted_publication_principal(migrated_url: str) -> None:
    request = replace(
        _PUBLIC_REQUEST,
        visibility=ImageVisibility.PRIVATE,
        owner="proj",
        expires_at=_DT + timedelta(days=1),
    )

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await reserve_publish(conn, request, size_bytes=len(_QCOW2), principal="alice")
            second = await reserve_publish(conn, request, size_bytes=len(_QCOW2), principal="bob")
            row = await IMAGE_CATALOG.get(conn, second.row_id)
            assert row is not None
            assert first.row_id == second.row_id
            assert row.publication_principal == "bob"

    asyncio.run(_run())


def test_private_reservation_requires_a_principal(migrated_url: str) -> None:
    request = replace(
        _PUBLIC_REQUEST,
        visibility=ImageVisibility.PRIVATE,
        owner="proj",
        expires_at=_DT + timedelta(days=1),
    )

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="private image reservation requires a principal"):
                await reserve_publish(conn, request, size_bytes=len(_QCOW2))

    asyncio.run(_run())


def test_registration_clears_publication_attempt_and_principal(
    migrated_url: str, tmp_path: Path
) -> None:
    request = replace(
        _PUBLIC_REQUEST,
        visibility=ImageVisibility.PRIVATE,
        owner="proj",
        expires_at=_DT + timedelta(days=1),
    )
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            reservation = await reserve_publish(
                conn, request, size_bytes=len(_QCOW2), principal="alice"
            )
            config_written = await write_publish_object(_FakeStore(), reservation, source)
            entry = await finish_publish(conn, reservation, config_written=config_written)
            assert entry.publication_attempt_id is None
            assert entry.publication_principal is None

    asyncio.run(_run())


def test_catalog_projections_withhold_publication_attempt_fields() -> None:
    from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS
    from kdive.mcp.tools.catalog.images import _describe_envelope, _row_envelope

    entry = ImageCatalogEntry(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        pending_since=_DT,
        provider="local-libvirt",
        name="base",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        object_key="images/local-libvirt/base/x86_64.qcow2",
        digest=_DIGEST,
        visibility=ImageVisibility.PUBLIC,
        state=ImageState.PENDING,
        publication_attempt_id=uuid4(),
        publication_principal="alice",
    )
    for envelope in (_row_envelope(entry), _describe_envelope(entry, DEFAULT_KERNEL_BASIS)):
        assert "publication_attempt_id" not in envelope.data
        assert "publication_principal" not in envelope.data


def test_realizing_defined_baseline_follows_same_path(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            seeded = ImageCatalogEntry(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                pending_since=_DT,
                provider="local-libvirt",
                name="base",
                arch="x86_64",
                format="qcow2",
                root_device="/dev/vda",
                object_key=None,
                digest=None,
                capabilities=[Capability.AGENT],
                provenance={},
                visibility=ImageVisibility.PUBLIC,
                owner=None,
                expires_at=None,
                state=ImageState.DEFINED,
            )
            inserted = await IMAGE_CATALOG.insert(conn, seeded)

            entry = await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
            # The seeded defined row is realized in place (defined -> pending -> registered).
            assert entry.id == inserted.id
            assert entry.state is ImageState.REGISTERED
            assert len(await IMAGE_CATALOG.list_all(conn)) == 1

    asyncio.run(_run())


def test_publish_does_not_clobber_operator_description(migrated_url: str, tmp_path: Path) -> None:
    # description is reconcile-owned (ADR-0311): a build/publish of the same image must leave an
    # operator-set description intact, since publish never writes that column.
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            seeded = ImageCatalogEntry(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                pending_since=_DT,
                provider="local-libvirt",
                name="base",
                arch="x86_64",
                format="qcow2",
                root_device="/dev/vda",
                object_key=None,
                digest=None,
                capabilities=[Capability.AGENT],
                provenance={},
                visibility=ImageVisibility.PUBLIC,
                owner=None,
                expires_at=None,
                state=ImageState.DEFINED,
                description="operator hint: RHEL debug host",
            )
            await IMAGE_CATALOG.insert(conn, seeded)
            entry = await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
            assert entry.state is ImageState.REGISTERED
            assert entry.description == "operator hint: RHEL debug host"

    asyncio.run(_run())


def test_publish_fails_when_object_does_not_head(migrated_url: str, tmp_path: Path) -> None:
    # The put "succeeds" but the object is not actually present: the HEAD gate must catch it
    # and the row stays pending (no false registered).
    store = _FakeStore(drop_object=True)
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            message = str(err.value)
            assert message.startswith("published image object is not present after write")
            assert "HEAD gate failed" in message
            row = (await IMAGE_CATALOG.list_all(conn))[0]
            assert row.state is ImageState.PENDING
            assert err.value.details == {"object_key": row.object_key}

    asyncio.run(_run())


def test_publish_rejects_source_digest_mismatch(migrated_url: str, tmp_path: Path) -> None:
    # The declared digest disagrees with the source bytes: publish must fail-fast (a registered
    # row with a mismatched digest would be permanently unfetchable), leaving an adoptable pending.
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    wrong_digest = "sha256:" + "f" * 64

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await publish_image(
                    conn,
                    store,
                    request=replace(_PUBLIC_REQUEST, digest=wrong_digest),
                    source=source,
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            message = str(err.value)
            assert message.startswith("published image bytes do not match the declared content")
            # The structured details name the declared vs actually-computed digest for the agent.
            assert err.value.details == {"declared": wrong_digest, "actual": _DIGEST}
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len(rows) == 1
            assert rows[0].state is ImageState.PENDING
            assert store.puts == []  # rejected before any object write

    asyncio.run(_run())


def test_two_owners_same_identity_do_not_collide(migrated_url: str, tmp_path: Path) -> None:
    # Two projects publish a private image of the same (provider, name, arch). They must NOT adopt
    # each other's row and must NOT share one object key — cross-tenant isolation.
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await publish_image(
                conn,
                store,
                request=replace(
                    _PUBLIC_REQUEST,
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-a",
                    expires_at=expires,
                ),
                source=source,
                principal="alice",
            )
            b = await publish_image(
                conn,
                store,
                request=replace(
                    _PUBLIC_REQUEST,
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-b",
                    expires_at=expires,
                ),
                source=source,
                principal="bob",
            )
            assert a.id != b.id
            assert a.object_key != b.object_key
            assert a.owner == "proj-a"
            assert b.owner == "proj-b"
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len([r for r in rows if r.state is ImageState.REGISTERED]) == 2
            # Each owner resolves only its own private image.
            resolved_a = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-a")
            resolved_b = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b")
            assert resolved_a is not None and resolved_a.id == a.id
            assert resolved_b is not None and resolved_b.id == b.id

    asyncio.run(_run())


def test_public_publish_does_not_adopt_a_private_pending(migrated_url: str, tmp_path: Path) -> None:
    # A crashed private pending row for an identity must not be adopted by a public publish of the
    # same (provider, name, arch) — the match is scoped by visibility/owner.
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failing = _FakeStore(fail_put=True)
            with pytest.raises(CategorizedError):
                await publish_image(
                    conn,
                    failing,
                    request=replace(
                        _PUBLIC_REQUEST,
                        visibility=ImageVisibility.PRIVATE,
                        owner="proj-a",
                        expires_at=expires,
                    ),
                    source=source,
                    principal="alice",
                )
            private_pending = (await IMAGE_CATALOG.list_all(conn))[0]

            healthy = _FakeStore()
            public = await publish_image(conn, healthy, request=_PUBLIC_REQUEST, source=source)
            assert public.id != private_pending.id
            assert public.visibility is ImageVisibility.PUBLIC
            # The private pending row is untouched (still pending, still owned by proj-a).
            still = await IMAGE_CATALOG.get(conn, private_pending.id)
            assert still is not None
            assert still.state is ImageState.PENDING
            assert still.owner == "proj-a"

    asyncio.run(_run())


def test_private_publish_records_owner_and_expiry(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(
                conn,
                store,
                request=replace(
                    _PUBLIC_REQUEST,
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj",
                    expires_at=expires,
                ),
                source=source,
                principal="alice",
            )
            assert entry.visibility is ImageVisibility.PRIVATE
            assert entry.owner == "proj"
            assert entry.expires_at == expires
            # A private image resolves for its owner, not for another project.
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="proj") is not None
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="other") is None

    asyncio.run(_run())


_CONFIG = b"# CONFIG_X is not set\nCONFIG_Y=y\n"


def test_publish_writes_config_object_and_sets_key(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    request = replace(_PUBLIC_REQUEST, kernel_config=_CONFIG)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=request, source=source)
            assert entry.kernel_config_key is not None
            assert store._objects[entry.kernel_config_key] == _CONFIG

    asyncio.run(_run())


def test_write_config_best_effort_skips_without_key_or_config() -> None:
    async def _run() -> None:
        # A None config_key means no config was captured: nothing is written, presence is False.
        no_key_store = _FakeStore()
        with_config = replace(_PUBLIC_REQUEST, kernel_config=_CONFIG)
        assert await _write_config_best_effort(no_key_store, with_config, None) is False
        assert no_key_store.puts == []
        # A request carrying no kernel_config likewise writes nothing and reports False.
        no_config_store = _FakeStore()
        assert (
            await _write_config_best_effort(no_config_store, _PUBLIC_REQUEST, "some/key.config")
            is False
        )
        assert no_config_store.puts == []

    asyncio.run(_run())


def test_publish_without_config_sets_no_key(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=_PUBLIC_REQUEST, source=source)
            assert entry.kernel_config_key is None
            assert len(store._objects) == 1  # qcow2 only, no .config sibling

    asyncio.run(_run())


def test_config_write_failure_registers_without_config(migrated_url: str, tmp_path: Path) -> None:
    # A store whose .config put fails models a best-effort config leg failure (ADR-0317).
    store = _FakeStore(fail_config=True)
    source = _qcow2_source(tmp_path)
    request = replace(_PUBLIC_REQUEST, kernel_config=_CONFIG)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=request, source=source)
            assert entry.state is ImageState.REGISTERED  # image still publishes
            assert entry.kernel_config_key is None  # key cleared on best-effort failure
            assert entry.object_key is not None  # qcow2 present and referenced
            assert store.head(entry.object_key) is not None

    asyncio.run(_run())


def test_arbitrary_config_is_stored_unvalidated(migrated_url: str, tmp_path: Path) -> None:
    # kdive never validates the offered config: bytes the old server-build gate would reject
    # round-trip byte-identical (ADR-0316/0317).
    weird = b"# CONFIG_SQUASHFS is not set\nCONFIG_NONSENSE=42\n"
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    request = replace(_PUBLIC_REQUEST, kernel_config=weird)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=request, source=source)
            assert entry.kernel_config_key is not None
            assert store._objects[entry.kernel_config_key] == weird

    asyncio.run(_run())
