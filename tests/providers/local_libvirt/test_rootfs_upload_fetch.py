"""Investigation-scoped uploaded-rootfs provision-time fetch (ADR-0441, ADR-0434, ADR-0438)."""

from __future__ import annotations

import base64
import errno
import gzip
import hashlib
import io
import multiprocessing as mp
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.artifacts.content_address import rootfs_object_name, rootfs_object_token
from kdive.artifacts.storage import HeadResult, StreamedArtifact
from kdive.db.locks import _session_lock_key
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    staged_rootfs_path,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import (
    _STREAM_CHUNK_BYTES,
    _fetch_lock_name,
    fetch_uploaded_rootfs,
    stage_uploaded_rootfs,
)
from kdive.store.objectstore import artifact_key

# A minimal canonical qcow2 base: the magic followed by arbitrary body bytes.
_QCOW2 = b"QFI\xfb" + b"canonical-qcow2-body"
# A valid canonical base64 SHA-256 (32 bytes); the profile's content-address handle.
_CHECKSUM = base64.b64encode(bytes(range(32))).decode("ascii")
_TOKEN = rootfs_object_token(_CHECKSUM)


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class _RecordingReader:
    """A body reader that records the largest single ``bytes`` it ever handed the caller.

    ``largest_read`` is the peak-buffered-bytes measurement the streaming requirement is stated in:
    it is what the staging loop holds in memory at once. A sizeless ``read()`` / ``read(-1)`` — the
    whole-object drain — is rejected outright rather than served, so a regression to buffering the
    object fails here instead of quietly passing a bound computed over internal chunking.

    ``max_chunk`` serves short reads (fewer bytes than asked, not end-of-stream), which the real
    store's ``RawIOBase`` body wrapper is free to do.
    """

    def __init__(self, data: bytes, *, max_chunk: int | None = None) -> None:
        self._data = data
        self._offset = 0
        self._max_chunk = max_chunk
        self.largest_read = 0

    @property
    def bytes_served(self) -> int:
        return self._offset

    def read(self, size: int | None = None, /) -> bytes:
        if size is None or size < 0:
            raise AssertionError(
                "the staging path asked for the whole object in one read; it must read in "
                "bounded chunks so peak memory does not scale with the object size (#1520)"
            )
        if self._max_chunk is not None:
            size = min(size, self._max_chunk)
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        self.largest_read = max(self.largest_read, len(chunk))
        return chunk

    def close(self) -> None:
        return None


class _FakeStore:
    def __init__(
        self, data: bytes | None, *, checksum: str | None, max_chunk: int | None = None
    ) -> None:
        self._data = data
        self._checksum = checksum
        self._max_chunk = max_chunk
        self.head_calls = 0
        self.range_calls = 0
        self.stream_calls = 0
        self.readers: list[_RecordingReader] = []

    def head(self, key: str) -> HeadResult | None:
        self.head_calls += 1
        if self._data is None:
            return None
        return HeadResult(size_bytes=len(self._data), checksum_sha256=self._checksum, etag="e")

    @contextmanager
    def get_artifact_stream(self, key: str, etag: str | None) -> Iterator[StreamedArtifact]:
        self.stream_calls += 1
        assert self._data is not None
        reader = _RecordingReader(self._data, max_chunk=self._max_chunk)
        self.readers.append(reader)
        try:
            yield StreamedArtifact(cast(IO[bytes], reader), Sensitivity.SENSITIVE, "rootfs")
        finally:
            reader.close()

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        self.range_calls += 1
        assert self._data is not None
        return self._data[start : start + length]


class _FailingReader:
    """A reader that serves ``fail_after`` bytes, then raises ``error`` mid-stream."""

    def __init__(self, data: bytes, *, fail_after: int, error: BaseException) -> None:
        self._data = data
        self._fail_after = fail_after
        self._error = error
        self._offset = 0

    def read(self, size: int | None = None, /) -> bytes:
        if self._offset >= self._fail_after:
            raise self._error
        want = self._fail_after - self._offset if size is None else min(size, self._fail_after)
        chunk = self._data[self._offset : self._offset + want]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


class _FailingStreamStore:
    """A store whose body read faults mid-stream, after the partial has bytes in it."""

    def __init__(self, data: bytes, *, fail_after: int, error: BaseException | None = None) -> None:
        self._data = data
        self._fail_after = fail_after
        self._error = error or CategorizedError(
            "get_object on 'k' failed: ConnectionClosedError",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )

    def head(self, key: str) -> HeadResult:
        return HeadResult(
            size_bytes=len(self._data), checksum_sha256=_sha256_b64(self._data), etag="e"
        )

    @contextmanager
    def get_artifact_stream(self, key: str, etag: str | None) -> Iterator[StreamedArtifact]:
        reader = _FailingReader(self._data, fail_after=self._fail_after, error=self._error)
        try:
            yield StreamedArtifact(cast(IO[bytes], reader), Sensitivity.SENSITIVE, "rootfs")
        finally:
            reader.close()

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        raise AssertionError("the identity path must not use ranged reads")


def _dest(tmp_path: Path) -> Path:
    return tmp_path / f"{_TOKEN}.qcow2"


def _stage(store: _FakeStore, tmp_path: Path, **kw: Any) -> Path:
    dest = _dest(tmp_path)
    stage_uploaded_rootfs(
        store,
        object_key="local/investigations/inv/rootfs-token",
        dest=dest,
        system_id=uuid4(),
        **kw,
    )
    return dest


# --- stage_uploaded_rootfs: identity path (checksum + qcow2-magic + unique partial) --------------


def test_stage_downloads_and_stages_verified_bytes(tmp_path: Path) -> None:
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    assert dest.read_bytes() == _QCOW2
    assert list(dest.parent.glob(f"{_TOKEN}.*.partial")) == []  # unique partial cleaned up


def test_stage_missing_object_is_config_error(tmp_path: Path) -> None:
    store = _FakeStore(None, checksum=None)
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "never uploaded" in str(error.value)
    assert store.stream_calls == 0


def test_stage_object_without_checksum_is_rejected(tmp_path: Path) -> None:
    store = _FakeStore(b"x", checksum=None)
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "no stored checksum" in str(error.value)
    assert store.stream_calls == 0


def test_stage_checksum_mismatch_is_infra_error_and_stages_nothing(tmp_path: Path) -> None:
    store = _FakeStore(b"actual-bytes", checksum=_sha256_b64(b"different-bytes"))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "checksum verification" in str(error.value)
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_corrupt_object_reports_the_checksum_gate_not_the_format_gate(tmp_path: Path) -> None:
    # Gate precedence (ADR-0438 §3, ADR-0441 §5). Store-side corruption usually destroys the qcow2
    # magic too, so gating on the unauthenticated first chunk would report this as the *format*
    # gate ("upload a qcow2 image", CONFIGURATION_ERROR) instead of the checksum gate. The checksum
    # is compared first, so the object is fully drained and the failure keeps the category
    # ADR-0434 §2 decided for it. This pins the gate that fires, not the wider retryability
    # question the gzip path answers differently (#1523).
    payload = b"\x00\x00\x00\x00corrupted-head" + b"\xa5" * (_STREAM_CHUNK_BYTES * 2)
    store = _FakeStore(payload, checksum=_sha256_b64(b"the-uncorrupted-object"))

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "checksum verification" in str(error.value)
    assert store.readers[0].bytes_served == len(payload)  # hashed over the whole object
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_non_qcow2_identity_is_config_error_and_stages_nothing(tmp_path: Path) -> None:
    data = b"not-a-qcow2-image"  # correct checksum but wrong format
    store = _FakeStore(data, checksum=_sha256_b64(data))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "qcow2" in str(error.value)
    assert not _dest(tmp_path).exists()


def test_stage_identity_sentinel_stages_verbatim(tmp_path: Path) -> None:
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _stage(store, tmp_path, encoding="identity", uncompressed_size=None)
    assert dest.read_bytes() == _QCOW2
    assert store.range_calls == 0


def test_stage_identity_streams_without_buffering_the_whole_object(tmp_path: Path) -> None:
    # #1520: peak memory is bounded by the read chunk, not the object size. The store fake offers
    # no whole-object accessor at all, so a regression to ``get_artifact(...).data`` cannot even
    # run. The payload size is a literal and the bound is asserted against it, not against
    # _STREAM_CHUNK_BYTES -- comparing the constant to itself would hold for any chunk size,
    # including one large enough to re-introduce the multi-GiB spike this test exists to prevent.
    assert _STREAM_CHUNK_BYTES <= 16 * 1024 * 1024  # the ceiling the memory claim rests on
    payload = _QCOW2 + b"\xa5" * (48 * 1024 * 1024)
    store = _FakeStore(payload, checksum=_sha256_b64(payload))

    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == payload
    assert store.stream_calls == 1  # one GET, not a per-chunk ranged-read fan-out
    assert store.readers[0].largest_read < len(payload)  # never held the object in one buffer
    assert store.readers[0].largest_read <= 16 * 1024 * 1024


def test_stage_identity_tolerates_reads_shorter_than_the_magic(tmp_path: Path) -> None:
    # The real body wrapper is a RawIOBase: read() may return fewer bytes than asked without being
    # end-of-stream, so the magic peek must accumulate across chunks rather than assume the first
    # read holds all four bytes.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2), max_chunk=3)

    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    assert store.readers[0].largest_read == 3  # the staging loop really did see short reads


def test_stage_identity_shorter_than_the_magic_is_config_error(tmp_path: Path) -> None:
    # An object too short to hold the magic can never be a qcow2; it must be rejected by the format
    # gate rather than staged because the peek never completed.
    data = b"QF"
    store = _FakeStore(data, checksum=_sha256_b64(data))

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "qcow2" in str(error.value)
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_identity_transport_fault_leaves_no_partial(tmp_path: Path) -> None:
    # A mid-stream transport fault (the typed INFRASTRUCTURE_FAILURE the store's body wrapper
    # raises) must discard the partial the loop had already written.
    store = _FailingStreamStore(_QCOW2 + b"\xa5" * 8192, fail_after=4096)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)  # ty: ignore[invalid-argument-type]

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_untyped_interrupt_still_leaves_no_partial(tmp_path: Path) -> None:
    # The partial now exists for the whole multi-GiB download, not just the final write, so the
    # discard must cover any exception that unwinds the frame — not only the store's typed
    # taxonomy — or a SENSITIVE multi-GiB orphan waits for some later fetch of the same base to
    # sweep it (ADR-0441 §5 specifies a `finally`). KeyboardInterrupt stands in as a BaseException
    # that no `except Exception` would catch. A *killed* worker is a different case and is not
    # covered here: it unwinds nothing, and the sweeps collect it.
    store = _FailingStreamStore(_QCOW2 + b"\xa5" * 8192, fail_after=4096, error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)  # ty: ignore[invalid-argument-type]

    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_io_fault_maps_to_a_staging_fault_naming_the_destination(tmp_path: Path) -> None:
    # The write loop now runs for the length of a multi-GiB transfer, so an ENOSPC/EIO mid-stage is
    # the realistic IO failure. It must leave the seam as the uniform INFRASTRUCTURE_FAILURE naming
    # `dest`, not a bare OSError -- this pins `except OSError -> _staging_fault`, which no longer
    # carries the `_discard` call that used to make it visibly load-bearing.
    store = _FailingStreamStore(
        _QCOW2 + b"\xa5" * 8192,
        fail_after=4096,
        error=OSError(errno.ENOSPC, "No space left on device"),
    )

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)  # ty: ignore[invalid-argument-type]

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "failed to stage" in str(error.value)
    assert error.value.details["dest"] == str(_dest(tmp_path))
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_unsupported_encoding_is_config_error(tmp_path: Path) -> None:
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="zstd", uncompressed_size=999)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "unsupported transport encoding" in str(error.value)
    assert store.stream_calls == 0 and store.range_calls == 0
    assert not _dest(tmp_path).exists()


# --- stage_uploaded_rootfs: gzip transport-strip path (ADR-0438) --------------------------------


def test_stage_gzip_streams_decompressed_qcow2(tmp_path: Path) -> None:
    canonical = _QCOW2 + b"x" * 4096
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    dest = _stage(store, tmp_path, encoding="gzip", uncompressed_size=len(canonical))
    assert dest.read_bytes() == canonical
    assert store.stream_calls == 0 and store.range_calls >= 1  # ranged, never whole-object


def test_stage_gzip_bomb_is_rejected_and_stages_nothing(tmp_path: Path) -> None:
    canonical = _QCOW2 + b"y" * 8192
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=64)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_gzip_non_qcow2_canonical_is_rejected(tmp_path: Path) -> None:
    canonical = b"decodes-fine-but-not-a-qcow2"
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=len(canonical))
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "qcow2" in str(error.value)
    assert not _dest(tmp_path).exists()


def test_stage_gzip_without_uncompressed_size_is_config_error(tmp_path: Path) -> None:
    compressed = gzip.compress(_QCOW2)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=None)
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "uncompressed_size" in str(error.value)


# --- fetch_uploaded_rootfs resolution + lock (fake sync connection, no DB) -----------------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._last: str | None = None
        self._row: tuple[object, ...] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if "FROM systems" in sql:
            self._row = (
                None if self._conn.investigation_id is None else (self._conn.investigation_id,)
            )
        elif "FROM artifacts" in sql:
            queried_key = params[2]
            self._conn.queried_object_keys.append(str(queried_key))
            self._row = (
                (self._conn.encoding, self._conn.uncompressed_size)
                if queried_key == self._conn.owned_object_key
                else None
            )

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeConn:
    """A minimal sync connection: seeds the two SELECT rows and records advisory-lock keys."""

    def __init__(
        self,
        *,
        investigation_id: UUID | None,
        owned_object_key: str | None = None,
        encoding: str | None = None,
        uncompressed_size: int | None = None,
    ) -> None:
        self.investigation_id = investigation_id
        self.owned_object_key = owned_object_key
        self.encoding = encoding
        self.uncompressed_size = uncompressed_size
        self.queried_object_keys: list[str] = []
        self.lock_keys: list[int] = []
        self.unlock_keys: list[int] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if "pg_advisory_lock" in sql:
            self.lock_keys.append(int(params[0]))
        elif "pg_advisory_unlock" in sql:
            self.unlock_keys.append(int(params[0]))


def _upload(tmp_path: Path, system_id: UUID | None = None) -> RootfsUploadContext:
    return RootfsUploadContext("local", system_id or uuid4(), tmp_path, _CHECKSUM)


def _owned_key(inv: UUID) -> str:
    return artifact_key("local", "investigations", str(inv), rootfs_object_name(_TOKEN))


def test_fetch_resolves_by_content_addressed_object_key(tmp_path: Path) -> None:
    # AC-4b: the profile's base64 checksum transcodes to the base64url token and the derived
    # object_key resolves within the System's own investigation; the base stages once.
    inv = uuid4()
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    upload = _upload(tmp_path)

    result = fetch_uploaded_rootfs(conn, store, upload)  # ty: ignore[invalid-argument-type]

    assert result == staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    assert result.read_bytes() == _QCOW2
    # Resolved by the derived key (base64->base64url transcode is the single canonical mapping).
    assert conn.queried_object_keys == [_owned_key(inv)]
    assert _TOKEN != _CHECKSUM  # a real transcode happened


def test_fetch_unowned_checksum_is_isolation_config_error(tmp_path: Path) -> None:
    # AC-2: a System bound to investigation Y naming a checksum only investigation X owns misses
    # (owner_id predicate is the boundary) and stages nothing.
    inv_y = uuid4()
    conn = _FakeConn(investigation_id=inv_y, owned_object_key="a-different-owned-key")
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert _CHECKSUM in str(error.value.details)
    assert store.head_calls == 0
    assert not staged_rootfs_path(inv_y, _TOKEN, upload_dir=tmp_path).exists()


def test_fetch_unbound_system_is_config_error(tmp_path: Path) -> None:
    conn = _FakeConn(investigation_id=None)
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "investigation" in str(error.value)


def test_fetch_reads_gzip_encoding_from_the_resolved_row(tmp_path: Path) -> None:
    # AC-4c: the encoding is read from the durable artifacts row (finalize deleted the manifest);
    # a gzip row is gunzipped and the staged base is a valid qcow2, not verbatim gzip bytes.
    inv = uuid4()
    canonical = _QCOW2 + b"z" * 2048
    compressed = gzip.compress(canonical)
    conn = _FakeConn(
        investigation_id=inv,
        owned_object_key=_owned_key(inv),
        encoding="gzip",
        uncompressed_size=len(canonical),
    )
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))

    result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == canonical
    assert result.read_bytes()[:4] == b"QFI\xfb"


def test_fetch_reuses_present_file_without_touching_store(tmp_path: Path) -> None:
    # AC-1 (sequential): a present verified base is a cache hit; the store is never read again.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already-verified")
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result == dest and result.read_bytes() == b"already-verified"
    assert store.head_calls == 0 and store.stream_calls == 0
    assert conn.lock_keys == []  # the pre-lock cache hit never even took the lock


def test_fetch_uses_deterministic_session_lock_key_not_hash(tmp_path: Path) -> None:
    # Lock-key guard (REQUIRED): the fetch serializes on the SESSION advisory keyspace derived from
    # a stable per-(investigation, token) name — NOT Python hash() (per-process salted, which would
    # silently no-op the lock across worker processes and re-admit the double download).
    inv = uuid4()
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    expected = _session_lock_key(f"rootfs-fetch:{inv}:{_TOKEN}")
    assert _fetch_lock_name(inv, _TOKEN) == f"rootfs-fetch:{inv}:{_TOKEN}"
    assert conn.lock_keys == [expected]
    assert conn.unlock_keys == [expected]  # released after os.replace, before returning
    assert expected != (hash(f"rootfs-fetch:{inv}:{_TOKEN}") & 0x7FFF_FFFF_FFFF_FFFF)


def test_fetch_unlinks_crash_orphan_partials_under_the_lock(tmp_path: Path) -> None:
    # ADR-0441 §5: a killed worker's <token>.*.partial is glob-unlinked opportunistically on the
    # next fetch (under the serializing lock, so no live sibling partial can exist).
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    orphan = dest.parent / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked")
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert not orphan.exists()
    assert dest.read_bytes() == _QCOW2


# --- cross-process AC-1: two interpreters race the real advisory lock, one download --------------


class _CountingStore:
    """A real-store stand-in whose downloads append to a shared on-disk counter (cross-process)."""

    def __init__(self, data: bytes, counter: Path) -> None:
        self._data = data
        self._counter = counter

    def head(self, key: str) -> HeadResult:
        return HeadResult(
            size_bytes=len(self._data), checksum_sha256=_sha256_b64(self._data), etag="e"
        )

    @contextmanager
    def get_artifact_stream(self, key: str, etag: str | None) -> Iterator[StreamedArtifact]:
        with self._counter.open("a") as fh:
            fh.write("1\n")
        reader = io.BytesIO(self._data)
        try:
            yield StreamedArtifact(reader, Sensitivity.SENSITIVE, "rootfs")
        finally:
            reader.close()

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._data[start : start + length]


def _fetch_child(
    url: str, upload_dir: str, system_id: str, checksum: str, counter: str, barrier: Any
) -> None:  # pragma: no cover - runs in a child process
    store = _CountingStore(_QCOW2, Path(counter))
    upload = RootfsUploadContext("local", UUID(system_id), Path(upload_dir), checksum)
    barrier.wait(timeout=30)  # both children reach the fetch together, racing for the lock
    with psycopg.connect(url, autocommit=True) as conn:
        fetch_uploaded_rootfs(conn, store, upload)


async def _seed_bound_systems(url: str, inv: UUID, sys_a: UUID, sys_b: UUID, key: str) -> None:
    from tests.mcp.systems_support import granted_allocation, pool

    async with pool(url) as conn_pool:
        alloc = await granted_allocation(conn_pool, cap=4)
        async with conn_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO investigations (id, principal, project, title, state) "
                "VALUES (%s, %s, %s, %s, %s)",
                (inv, "p", "proj", "t", "open"),
            )
            for sid in (sys_a, sys_b):
                await conn.execute(
                    "INSERT INTO systems (id, allocation_id, state, provisioning_profile, "
                    "principal, project, investigation_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (sid, alloc, "provisioning", Jsonb({"kind": "upload"}), "p", "proj", inv),
                )
            await conn.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) VALUES ('investigations', %s, %s, 'e', 'sensitive', 'rootfs')",
                (inv, key),
            )


def test_two_processes_share_one_download(migrated_url: str, tmp_path: Path) -> None:
    # AC-1 (cross-process, the deterministic-lock proof): two interpreters sharing the DATABASE_URL
    # and staging dir race for the base; the session advisory lock serializes them so exactly ONE
    # downloads and the second is a cache hit. This fails against a hash()-keyed lock (each process
    # would derive a different key, no serialization, two downloads).
    import asyncio

    inv, sys_a, sys_b = uuid4(), uuid4(), uuid4()
    key = artifact_key("local", "investigations", str(inv), rootfs_object_name(_TOKEN))
    asyncio.run(_seed_bound_systems(migrated_url, inv, sys_a, sys_b, key))

    counter = tmp_path / "downloads.log"
    counter.write_text("")
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [
        ctx.Process(
            target=_fetch_child,
            args=(migrated_url, str(tmp_path), str(sid), _CHECKSUM, str(counter), barrier),
        )
        for sid in (sys_a, sys_b)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    assert counter.read_text().count("1") == 1  # exactly one download served both Systems
    assert staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path).read_bytes() == _QCOW2
