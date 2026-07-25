"""Investigation-scoped uploaded-rootfs provision-time fetch (ADR-0441, ADR-0434, ADR-0438)."""

from __future__ import annotations

import base64
import errno
import fcntl
import gzip
import hashlib
import io
import logging
import multiprocessing as mp
import os
import stat
import threading
import time
import types
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
from kdive.providers.local_libvirt.lifecycle.rootfs import rootfs_upload_fetch
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    staged_rootfs_path,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import (
    _STAGING_FREE_SPACE_MARGIN_BYTES,
    _STREAM_CHUNK_BYTES,
    _fetch_lock_name,
    _unlink_orphan_partials,
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
    store's ``RawIOBase`` body wrapper is free to do. ``fail`` is an ``(offset, error)`` pair that
    raises mid-stream once that many bytes have been served, with the partial already on disk.
    """

    def __init__(
        self,
        data: bytes,
        *,
        max_chunk: int | None = None,
        fail: tuple[int, BaseException] | None = None,
    ) -> None:
        self._data = data
        self._offset = 0
        self._max_chunk = max_chunk
        self._fail = fail
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
        if self._fail is not None:
            at, error = self._fail
            if self._offset >= at:
                raise error
            size = min(size, at - self._offset)
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
        self,
        data: bytes | None,
        *,
        checksum: str | None,
        max_chunk: int | None = None,
        fail: tuple[int, BaseException] | None = None,
    ) -> None:
        self._data = data
        self._checksum = checksum
        self._max_chunk = max_chunk
        self._fail = fail
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
        reader = _RecordingReader(self._data, max_chunk=self._max_chunk, fail=self._fail)
        self.readers.append(reader)
        try:
            yield StreamedArtifact(cast(IO[bytes], reader), Sensitivity.SENSITIVE, "rootfs")
        finally:
            reader.close()

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        self.range_calls += 1
        assert self._data is not None
        return self._data[start : start + length]


def _store_failing_mid_stream(error: BaseException) -> _FakeStore:
    """A store whose body read faults after 4 KiB, with bytes already in the partial."""
    data = _QCOW2 + b"\xa5" * 8192
    return _FakeStore(data, checksum=_sha256_b64(data), fail=(4096, error))


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


def _ample_free_space(_path: Any) -> Any:
    """A ``disk_usage`` stand-in with room for any payload in this module above the #1525 floor."""
    ample = _STAGING_FREE_SPACE_MARGIN_BYTES + 64 * 1024**2
    return types.SimpleNamespace(total=ample, used=0, free=ample)


@pytest.fixture(autouse=True)
def _staging_volume_has_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the staging filesystem ample for every test here, so none depends on the host's disk.

    The free-space precheck runs on every ``stage_uploaded_rootfs`` call that knows its required
    size — which is most of this module, not just the handful of tests that are *about* the
    precheck. Without this pin, a runner whose pytest tmp filesystem sits under the 1 GiB floor
    (a tmpfs ``/tmp`` in a small container is exactly that) would turn three dozen otherwise
    deterministic tests red, and red with an unrelated "not enough free space" error rather than
    the one each test asserts. The precheck's own tests call ``_pin_free_space`` from inside the
    test body, i.e. after this fixture, and monkeypatch's later write wins — so they still choose
    their own figure.
    """
    monkeypatch.setattr(rootfs_upload_fetch, "disk_usage", _ample_free_space)


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
    payload = b"\x00\x00\x00\x00corrupted-head" + b"\xa5" * 500
    store = _FakeStore(payload, checksum=_sha256_b64(b"the-uncorrupted-object"), max_chunk=8)

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
    # run. The peak-read bound is asserted against a LITERAL, not against _STREAM_CHUNK_BYTES:
    # comparing the observed read to the constant the code passes would hold for any chunk size,
    # including one large enough to re-introduce the spike this test exists to prevent. The two
    # bounds are deliberately independent -- the first pins the constant, the second pins what the
    # staging loop actually asks for, so neither the constant nor the call site can drift alone.
    assert _STREAM_CHUNK_BYTES <= 16 * 1024 * 1024  # the ceiling the memory claim rests on
    payload = _QCOW2 + b"\xa5" * (20 * 1024 * 1024)
    store = _FakeStore(payload, checksum=_sha256_b64(payload))

    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == payload
    assert store.stream_calls == 1  # one GET, not a per-chunk ranged-read fan-out
    assert store.readers[0].largest_read <= 16 * 1024 * 1024
    assert store.readers[0].largest_read < len(payload)  # never held the object in one buffer


def test_stage_identity_tolerates_short_reads(tmp_path: Path) -> None:
    # The real body wrapper is a RawIOBase: read() may return fewer bytes than asked without being
    # end-of-stream, so a short chunk must not be mistaken for EOF and truncate the staged base.
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
    store = _store_failing_mid_stream(
        CategorizedError(
            "get_object on 'k' failed: ConnectionClosedError",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    )

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

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
    store = _store_failing_mid_stream(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_io_fault_maps_to_a_staging_fault_naming_the_destination(tmp_path: Path) -> None:
    # The write loop now runs for the length of a multi-GiB transfer, so an ENOSPC/EIO mid-stage is
    # the realistic IO failure. It must leave the seam as the uniform INFRASTRUCTURE_FAILURE naming
    # `dest`, not a bare OSError -- this pins `except OSError -> _staging_fault`, which no longer
    # carries the `_discard` call that used to make it visibly load-bearing.
    store = _store_failing_mid_stream(OSError(errno.ENOSPC, "No space left on device"))

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

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


# --- stage_uploaded_rootfs: the staging free-space precheck (#1525) ------------------------------


def _pin_free_space(monkeypatch: pytest.MonkeyPatch, free: int) -> list[Path]:
    """Pin the staging filesystem's free bytes; return the paths the precheck measured.

    The measured paths are returned rather than discarded because *which* filesystem is asked is
    half the contract: the precheck must stat the directory the base actually lands in, not the
    process CWD or the overlay directory, or it would license a write onto a volume it never looked
    at.

    Patches the module's own ``disk_usage`` name, which is why the module imports the function
    rather than ``shutil``: patching ``rootfs_upload_fetch.shutil.disk_usage`` would reach the
    stdlib module object itself and replace it for every importer in the process.
    """
    measured: list[Path] = []

    def _usage(path: Any) -> Any:
        measured.append(Path(path))
        return types.SimpleNamespace(total=free, used=0, free=free)

    monkeypatch.setattr(rootfs_upload_fetch, "disk_usage", _usage)
    return measured


def test_stage_identity_precheck_rejects_an_object_that_cannot_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Since streaming landed (#1520) a rejected object is written in FULL before anything rejects
    # it, onto a filesystem that by default also holds every live System's overlay. So the object
    # must be refused before the first byte, and the refusal must name both numbers an operator
    # acts on -- how much is needed and how much is there.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    measured = _pin_free_space(monkeypatch, free=len(_QCOW2) - 1)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(len(_QCOW2) + _STAGING_FREE_SPACE_MARGIN_BYTES) in str(error.value)
    assert str(len(_QCOW2) - 1) in str(error.value)
    assert "does not fit at all" in str(error.value)
    assert store.stream_calls == 0  # refused before the download, not after it
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []
    assert measured == [tmp_path]  # the directory the base lands in


def test_stage_precheck_details_decompose_the_figures_the_message_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `details` is what a dashboard, a triage script, or an agent reads instead of the English --
    # the worker copies these scalars into the job's `failure_context`. A bare "needs N, has M"
    # would re-derive exactly the misattribution the split message exists to remove, since N folds
    # the 1 GiB floor into what reads as the object's size. Every figure in the prose has a key,
    # and the `reason` slug must agree with the branch the prose took.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=len(_QCOW2) - 1)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    details = error.value.details
    assert details["base_bytes"] == len(_QCOW2)  # the object, NOT the object plus the floor
    assert details["floor_bytes"] == _STAGING_FREE_SPACE_MARGIN_BYTES
    assert details["needed_bytes"] == len(_QCOW2) + _STAGING_FREE_SPACE_MARGIN_BYTES
    assert details["free_bytes"] == len(_QCOW2) - 1
    assert details["reason"] == "base_does_not_fit"
    assert details["budget_source"] == "object_size"  # an exact size, not a declared bound


def test_stage_precheck_requires_headroom_beyond_the_base_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A check that admitted `free == size` would permit filling the volume to exactly zero bytes
    # free -- which is the degraded state for the sibling overlays this guard exists to protect,
    # not the avoidance of it. The margin is therefore load-bearing, and pinned here so it cannot
    # be quietly dropped to an exact-fit comparison.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=len(_QCOW2))

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert store.stream_calls == 0


def test_stage_precheck_says_which_of_the_two_conditions_refused_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Because the floor is a fixed volume-wide reserve, most refusals are NOT "your object is too
    # big": a 20-byte base against 900 MiB free would leave the volume almost untouched. A message
    # that blamed the object either way would point the operator at the upload instead of at the
    # floor that actually fired, so the two shapes must read differently.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=_STAGING_FREE_SPACE_MARGIN_BYTES - 1)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "the base itself fits; it is the floor that would be breached" in str(error.value)
    assert "does not fit at all" not in str(error.value)
    assert error.value.details["reason"] == "floor_breached"  # the slug tracks the prose branch


def test_stage_gzip_precheck_says_its_figure_is_a_declared_bound_not_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing checks `uncompressed_size` against the real decompressed length: the declaration
    # validator enforces only "positive, under the 50 GiB cap", `strip_gzip_to_writer` accepts less
    # than the bound, and the agent-facing schema calls it "the gzip-bomb bound" -- language that
    # invites rounding it up, since over-stating a bomb bound is safe everywhere else. Over-
    # reserving is free for a reservation and NOT free for a refusal, so an agent that declared
    # 40 GiB for a 2 GiB image must not be told the host is out of disk with no hint of why.
    canonical = _QCOW2 + b"\x00" * 4096
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    overstated = 40 * 1024**3
    _pin_free_space(monkeypatch, free=8 * 1024**3)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=overstated)

    assert error.value.details["budget_source"] == "uncompressed_size_bound"
    assert error.value.details["base_bytes"] == overstated
    assert "declared uncompressed_size upper bound rather than a measured size" in str(error.value)
    assert "re-declare the upload with the real decompressed size" in str(error.value)


def test_stage_identity_precheck_does_not_blame_a_declaration_it_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The converse of the test above: the identity figure IS a measurement (the object's stored
    # size), so telling the operator to re-declare it would send them after a field the declaration
    # validator rejects outright on this path.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=0)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert "uncompressed_size" not in str(error.value)


def test_stage_precheck_logs_the_refusal_on_the_host_that_must_act_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The remedy is entirely host-side, but the raised error reaches an operator only as one more
    # INFRASTRUCTURE_FAILURE among many -- so without a log line, someone watching a filling volume
    # cannot tell "provisions are being refused for capacity" from any other infra fault without
    # pulling each job's failure_context back through MCP.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=0)

    with (
        caplog.at_level(logging.WARNING, logger=rootfs_upload_fetch.__name__),
        pytest.raises(CategorizedError),
    ):
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert "refusing to stage an uploaded rootfs" in caplog.text
    assert str(tmp_path) in caplog.text
    assert "base_does_not_fit" in caplog.text


def test_stage_precheck_reports_free_space_as_the_figure_df_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `shutil.disk_usage().free` is statvfs f_bavail -- space available to UNPRIVILEGED users,
    # excluding the filesystem's reserved blocks. The staging worker often runs as root and could
    # write into that reserve, so the conservative number is deliberate; what must not happen is
    # reporting it as though it were the whole picture, leaving an operator unable to reconcile the
    # error with the shell. Naming df's column is what makes the number checkable.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=0)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert "df's Avail column" in str(error.value)


def test_stage_precheck_admits_a_base_that_fits_with_its_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other edge of the same boundary: exactly enough must pass. Without this the precheck
    # could be "fixed" into rejecting everything and the rejection tests above would still be green.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=len(_QCOW2) + _STAGING_FREE_SPACE_MARGIN_BYTES)

    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2


def test_stage_gzip_precheck_budgets_the_decompressed_bound_not_the_stored_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The compressed object is read through ranged GETs and never lands on disk; what occupies the
    # staging filesystem is the DECOMPRESSED output. Budgeting `head.size_bytes` on this path would
    # under-reserve by the whole compression ratio, so a volume with room for the stored object but
    # not for the canonical one must still be refused.
    canonical = _QCOW2 + b"\x00" * (4 * 1024 * 1024)
    compressed = gzip.compress(canonical)
    assert len(compressed) < len(canonical) // 8  # the ratio an under-reservation would hide
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    _pin_free_space(monkeypatch, free=len(compressed) + _STAGING_FREE_SPACE_MARGIN_BYTES)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=len(canonical))

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(len(canonical) + _STAGING_FREE_SPACE_MARGIN_BYTES) in str(error.value)
    assert store.range_calls == 0
    assert not _dest(tmp_path).exists()


def test_stage_precheck_defers_to_the_declaration_error_when_the_size_is_unknowable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `uncompressed_size` is the only size the gzip path can budget, so a declaration without it
    # leaves the requirement genuinely unknown. The precheck must skip rather than substitute the
    # stored size -- which under-reserves by construction -- and the actionable declaration error
    # must survive instead of being pre-empted by a free-space message about a number nobody knows.
    compressed = gzip.compress(_QCOW2)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))
    _pin_free_space(monkeypatch, free=0)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="gzip", uncompressed_size=None)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "uncompressed_size" in str(error.value)


def test_stage_precheck_defers_to_the_declaration_error_for_an_unsupported_codec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same "no knowable requirement" rule as the gzip case above, on the branch an agent reaches
    # by declaring a codec nothing implements. Free space is pinned to zero so a budget derived from
    # the stored size -- the tempting fallback -- would refuse here and mask the codec error, which
    # is the one an agent can act on. The module's ample-space fixture would hide that regression,
    # so the pin is what makes this branch's contract enforceable at all.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    _pin_free_space(monkeypatch, free=0)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding="zstd", uncompressed_size=999)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "unsupported transport encoding" in str(error.value)


def test_stage_precheck_stages_anyway_when_the_filesystem_cannot_be_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The precheck is advisory, so a precheck that cannot RUN must not become an outage: `statvfs`
    # can fail under the worker/staging-user asymmetry ADR-0442 documents in this same subsystem,
    # and refusing to stage then buys no safety at all -- the write is still guarded by the real
    # ENOSPC. This is the `_flocked_partial` ENOLCK precedent: degrade loudly to the prior behavior.
    def _raise(_path: object) -> object:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(rootfs_upload_fetch, "disk_usage", _raise)
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with caplog.at_level(logging.WARNING, logger=rootfs_upload_fetch.__name__):
        dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    assert "could not measure the free space" in caplog.text
    assert "Permission denied" in caplog.text


def test_stage_precheck_does_not_run_before_the_object_is_known_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ordering: an absent object has no size to budget, so the precheck must sit behind the HEAD.
    # A zero-free filesystem must still report "never uploaded" -- the agent's actionable fault --
    # rather than an operator-facing disk message for a download that was never going to happen.
    store = _FakeStore(None, checksum=None)
    measured = _pin_free_space(monkeypatch, free=0)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "never uploaded" in str(error.value)
    assert measured == []


# --- stage_uploaded_rootfs: crash durability of the publish (#1526) ------------------------------


def _raise_eio(fd: int) -> None:
    """An ``os.fsync`` stand-in for a disk that refuses the flush."""
    raise OSError(errno.EIO, "Input/output error")


def _trace_durability_syscalls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Record every ``fsync``/``replace`` as ``(op, inode)``, in call order, and keep doing them.

    The inode is what makes the trace meaningful: a rename preserves it, so the partial that was
    synced, the file that was published, and the base sitting at ``dest`` afterwards are all the
    same number, while the containing directory is a different one. That turns "durable before
    visible" — which is otherwise unobservable in-process, since it is only decided by a power cut
    — into an ordering assertion over the syscalls that decide it.
    """
    trace: list[tuple[str, int]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        trace.append(("fsync", os.fstat(fd).st_ino))
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        trace.append(("replace", Path(src).stat().st_ino))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    return trace


def test_stage_syncs_the_partial_before_publishing_then_syncs_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1526: `os.replace` is atomic against concurrent *readers*, not against a host crash. On a
    # default ext4 mount the rename can become durable while the data blocks behind it are not,
    # leaving a full-length `dest` of zeros or stale blocks. The partial's bytes must therefore be
    # fsynced BEFORE the rename publishes them, and the directory holding the rename fsynced after.
    # Order is the whole contract -- a sync after the rename would leave exactly the window the
    # bug describes -- so this pins the sequence, not merely that a sync happened.
    trace = _trace_durability_syscalls(monkeypatch)
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    base_inode = dest.stat().st_ino  # the rename preserved the partial's inode
    assert trace == [
        ("fsync", base_inode),  # the staged bytes are on the disk ...
        ("replace", base_inode),  # ... before the rename makes them reachable ...
        ("fsync", dest.parent.stat().st_ino),  # ... and the rename itself is then durable
    ]


def test_stage_gzip_also_syncs_the_partial_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gzip stager opens its own writer, so it needs the same durability as the identity one --
    # a sync wired into a single codec would leave the other lane crash-torn.
    trace = _trace_durability_syscalls(monkeypatch)
    canonical = _QCOW2 + b"g" * 4096
    compressed = gzip.compress(canonical)
    store = _FakeStore(compressed, checksum=_sha256_b64(compressed))

    dest = _stage(store, tmp_path, encoding="gzip", uncompressed_size=len(canonical))

    assert dest.read_bytes() == canonical
    base_inode = dest.stat().st_ino
    assert trace.index(("fsync", base_inode)) < trace.index(("replace", base_inode))
    assert trace[-1] == ("fsync", dest.parent.stat().st_ino)


def test_stage_partial_sync_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An fsync of the partial can fail for real (EIO, ENOSPC, EROFS after an error remount). It runs
    # before the rename, so the contract is that nothing is published and no partial is left.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    monkeypatch.setattr(os, "fsync", _raise_eio)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not _dest(tmp_path).exists()
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []


def test_stage_directory_sync_failure_leaves_the_good_base_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The directory fsync runs AFTER the rename, so its failure is the one case where the provision
    # fails with `dest` already published and intact. ADR-0443 section 1 states that outcome as
    # deliberate -- "the bytes are fine, only their durability is unproven" -- and it holds only
    # because the error path unlinks `partial` and never touches `dest`. Nothing but this test stops
    # a future cleanup from adding a `dest.unlink()` there and turning a benign failure into the
    # deletion of a base other Systems in the investigation may already be booting off.
    dest = _dest(tmp_path)
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    real_fsync = os.fsync

    def fail_on_the_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_on_the_directory)

    with pytest.raises(CategorizedError) as error:
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert dest.read_bytes() == _QCOW2  # published, intact, NOT rolled back
    assert list(tmp_path.glob(f"{_TOKEN}.*.partial")) == []

    # And the next fetch reuses it rather than re-downloading, which is what makes the outcome
    # "honest" rather than merely survivable.
    monkeypatch.setattr(os, "fsync", real_fsync)
    inv = uuid4()
    staged = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(dest.read_bytes())
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    reuse_store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    assert fetch_uploaded_rootfs(conn, reuse_store, _upload(tmp_path)) == staged  # ty: ignore[invalid-argument-type]
    assert reuse_store.stream_calls == 0


def test_stage_does_not_sync_a_partial_it_is_about_to_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sync costs a full flush of a base up to the 50 GiB canonical cap, so it belongs on the
    # publish path only: a partial that fails verification is unlinked in the `finally` and never
    # published, and paying to make those bytes durable buys nothing.
    #
    # BOTH rejections are covered, because they fail at different gates. The checksum arm raises
    # inside the stager; the format arm raises from the shared magic gate *after* the stager has
    # returned and closed its writer -- so a design that synced as each stager closed its writer
    # would still flush a 50 GiB raw/vmdk upload whose SHA-256 matched perfectly before rejecting
    # it. Syncing at the publish point instead is what makes the second case free.
    trace = _trace_durability_syscalls(monkeypatch)
    checksum_mismatch = _FakeStore(b"actual-bytes", checksum=_sha256_b64(b"different-bytes"))
    not_a_qcow2 = _FakeStore(
        b"a perfectly intact vmdk", checksum=_sha256_b64(b"a perfectly intact vmdk")
    )

    for store in (checksum_mismatch, not_a_qcow2):
        with pytest.raises(CategorizedError):
            _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert trace == []


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
        # The release path reports the OBSERVED connection state rather than an inferred cause,
        # so the fake carries the same flag a real psycopg connection exposes.
        self.closed = False

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
    dest.write_bytes(_QCOW2 + b"-already-staged")
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result == dest and result.read_bytes() == _QCOW2 + b"-already-staged"
    assert store.head_calls == 0 and store.stream_calls == 0
    assert conn.lock_keys == []  # the pre-lock cache hit never even took the lock


# --- fetch_uploaded_rootfs: the reuse fast path re-verifies before handing a base back (#1526) ---


def test_fetch_reuses_a_staged_base_without_consulting_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0450 claims the precheck "does not run on the reuse fast path at all", which is what keeps
    # a per-System provision hot path free of a `statvfs` and -- far more important -- keeps a full
    # volume from blocking Systems that need no space, because the base they want is already staged.
    # A volume with zero bytes free must still serve the reuse hit.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_QCOW2)
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    measured = _pin_free_space(monkeypatch, free=0)

    result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result == dest and result.read_bytes() == _QCOW2
    assert store.stream_calls == 0  # nothing was staged, so nothing needed space
    assert measured == []  # and the filesystem was never even asked


@pytest.mark.parametrize(
    ("name", "content"),
    [
        # A crash between the rename and the data blocks reaching disk: ext4 delayed allocation
        # leaves the file at its full length with zeros behind it. This is the shape #1526 is
        # named for, and the one a length- or existence-based gate cannot see.
        ("crash_torn_zeroes", b"\x00" * len(_QCOW2)),
        # A partial write that never reached the length of the magic.
        ("truncated_below_the_magic", b"QF"),
        # A zero-length file: `Path.touch()`, an ENOSPC at the first write, a restored-empty backup.
        ("empty", b""),
        # Not an image at all -- an error document or a log line written over the path.
        ("garbage", b"<Error><Code>NoSuchKey</Code></Error>"),
    ],
)
def test_fetch_rejects_an_unverifiable_staged_base_and_restages_it(
    tmp_path: Path, name: str, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    # #1526: the reuse fast path used to be `if dest.is_file(): return dest` -- it verified
    # NOTHING, so a base left corrupt by a crash mid-stage was handed straight back, and under
    # ADR-0441's content-addressed reuse it then backed every System in the investigation until
    # close. The checksum machinery was bypassed precisely BECAUSE the file existed. The observable
    # contract is that such a base never reaches the caller: it is re-fetched from the object store
    # and replaced, not returned.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(content)
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with caplog.at_level(logging.WARNING):
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == _QCOW2, f"{name}: handed back the unverifiable base"
    assert store.stream_calls == 1  # it really went back to the object store
    assert conn.lock_keys and conn.unlock_keys  # re-staged under the serializing fetch lock
    assert list(dest.parent.glob(f"{_TOKEN}.*.partial")) == []
    # The rejection is the one event this change exists to detect, and the re-stage succeeds, so a
    # log line is the ONLY signal it ever fired. Without it the fix is unmeasurable in production.
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "staged rootfs base at" in message and str(dest) in message for message in warnings
    ), f"{name}: rejected the base silently"
    # And it must NOT claim a concurrent sibling. Nothing between the pre-lock and post-lock checks
    # repairs `dest`, so an ungated post-lock warning would fire on every ordinary stale-base
    # rejection -- attributing the commonest case to a racing fetcher that never existed, and
    # making the genuinely concurrent case (the next test) indistinguishable from it.
    assert not any("a sibling published" in message for message in warnings), (
        f"{name}: blamed a sibling that never existed"
    )


def test_fetch_restages_over_a_non_regular_file_without_hanging(tmp_path: Path) -> None:
    # `dest.is_file()` rejected a non-regular file for free. The format probe `open`s instead, and
    # opening a FIFO for reading BLOCKS until a writer appears -- so a probe that did not check the
    # mode first would hang the provision thread forever, and the post-lock call site would hang
    # while HOLDING the fetch advisory lock, wedging every sibling System on that (investigation,
    # checksum). The connection is autocommit, so no idle_in_transaction_session_timeout breaks it.
    #
    # The fetch runs on a daemon thread with a bounded join rather than inline: without the S_ISREG
    # gate an inline call would hang the whole suite instead of failing this one test, and the repo
    # has no pytest-timeout to catch that.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    os.mkfifo(dest)
    assert not dest.is_file() and dest.exists()  # the shape the old gate rejected for free
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    outcome: list[Path | BaseException] = []

    def run() -> None:
        try:
            outcome.append(fetch_uploaded_rootfs(conn, store, _upload(tmp_path)))  # ty: ignore[invalid-argument-type]
        except BaseException as exc:  # noqa: BLE001  # reported on the main thread below
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "the reuse probe blocked on a non-regular file at the staged path"
    assert len(outcome) == 1 and isinstance(outcome[0], Path)
    assert outcome[0].read_bytes() == _QCOW2  # re-staged over it, as the old code did
    assert store.stream_calls == 1


def test_fetch_reports_an_unreadable_staged_base_instead_of_re_downloading_it(
    tmp_path: Path,
) -> None:
    # "I cannot read the staged base" is NOT "there is no staged base". The old `dest.is_file()`
    # was a stat, which needs only traverse permission on the parent; the format probe opens the
    # file, so it can also fail with EACCES (a worker/staging-user asymmetry of the shape ADR-0442
    # documents), EMFILE, or a transient EIO. Swallowing those as a cache miss would swap a good
    # multi-GiB base out from under any guest holding it open and re-download it on every single
    # provision for as long as the fault lasts -- silently. It must surface instead.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_QCOW2)
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    real_open = Path.open

    def refusing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == dest:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", refusing_open)
        with pytest.raises(CategorizedError) as error:
            fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert error.value.details["dest"] == str(dest)
    assert store.stream_calls == 0  # no re-download was attempted behind the operator's back
    assert dest.read_bytes() == _QCOW2  # and the good base was left exactly where it was
    # The message must point at the present-but-unreadable file, not at a download that was never
    # attempted: "failed to stage" would send an operator to the object store when the actionable
    # fix is the mode bits on a file that is already there.
    assert "present but could not be read" in str(error.value)
    assert "failed to stage" not in str(error.value)


class _SiblingStagesWhileWeWait(_FakeConn):
    """A connection whose lock acquisition stands in for a sibling fetcher finishing the stage.

    The pre-lock and post-lock reuse checks are separate call sites, so a re-check wired into only
    the first one would still hand back whatever the wait produced. Writing ``content`` at
    ``pg_advisory_lock`` reproduces exactly that window.
    """

    def __init__(self, *, dest: Path, content: bytes, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._dest = dest
        self._content = content

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        super().execute(sql, params)
        if "pg_advisory_lock" in sql:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            self._dest.write_bytes(self._content)


def test_fetch_reuses_a_sibling_staged_base_that_appeared_during_the_lock_wait(
    tmp_path: Path,
) -> None:
    # The post-lock fast path is the whole point of the lock: a sibling that finished while we
    # waited must be a cache hit, not a redundant multi-GiB download.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    conn = _SiblingStagesWhileWeWait(
        dest=dest,
        content=_QCOW2,
        investigation_id=inv,
        owned_object_key=_owned_key(inv),
    )
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == _QCOW2
    assert store.stream_calls == 0  # served by the post-lock re-check, no download
    assert conn.unlock_keys == conn.lock_keys  # and the lock was still released


def test_fetch_restages_over_a_torn_base_that_appeared_during_the_lock_wait(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The same window, but what appeared is a torn base rather than a good one -- the post-lock
    # check must verify it too, or a re-check on the pre-lock call site alone leaves the hole open
    # for every fetcher that had to queue.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    conn = _SiblingStagesWhileWeWait(
        dest=dest,
        content=b"\x00" * len(_QCOW2),
        investigation_id=inv,
        owned_object_key=_owned_key(inv),
    )
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with caplog.at_level(logging.WARNING):
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == _QCOW2
    assert store.stream_calls == 1  # the torn base was rejected and re-fetched
    # A sibling that just published something unverifiable is a louder signal than finding a stale
    # base on arrival, so the two rejection sites must not collapse into one indistinguishable line.
    assert any("a sibling published" in record.message for record in caplog.records)


def test_fetch_reuse_check_does_not_rescan_the_whole_base(tmp_path: Path) -> None:
    # The re-check runs on every guest start against a base that can be tens of GiB, so it must
    # stay O(1). A full checksum re-verify -- the obvious correct-but-unaffordable alternative --
    # would read the whole file, which this bounds directly rather than by inspecting the code.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_QCOW2 + b"\xa5" * (8 * 1024 * 1024))
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    reads: list[int] = []
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self == dest:
            real_read = handle.read

            def counting_read(size: int = -1, /) -> Any:
                data = real_read(size)
                reads.append(len(data))
                return data

            handle.read = counting_read
        return handle

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", counting_open)
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result == dest and store.stream_calls == 0  # reused
    assert sum(reads) <= 64, f"the reuse check read {sum(reads)} bytes of the base"


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
    # A spawned child re-imports the module fresh, so the parent's autouse free-space pin does not
    # reach it. Pin it here too, or this test would depend on the runner's real free disk (#1525).
    rootfs_upload_fetch.disk_usage = _ample_free_space  # ty: ignore[invalid-assignment]  # a stub
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


# --- #1524: the orphan sweep is gated on an flock, not on the (losable) fetch lock ---------------


@contextmanager
def _held_partial(dest: Path, suffix: str = "live") -> Iterator[Path]:
    """A ``<token>.<suffix>.partial`` held under an exclusive ``flock``, as a live fetcher holds it.

    Stands in for the sibling whose session advisory lock was dropped mid-download while it kept
    writing: from the sweeper's point of view the fetch lock is free and this file is present, which
    is exactly the state ADR-0441 §5 assumed could not occur.
    """
    partial = dest.parent / f"{dest.stem}.{suffix}.partial"
    partial.write_bytes(b"a live sibling is still writing this")
    fd = os.open(partial, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield partial
    finally:
        os.close(fd)


def test_sweep_skips_a_partial_a_live_sibling_still_holds(tmp_path: Path) -> None:
    # #1524 acceptance, at the sweep itself: the fetch lock is not evidence of anything, so the
    # sweep asks the kernel who is alive instead. Red before the fix -- the sweep glob-unlinked
    # unconditionally, destroying a multi-GiB download that was still in flight.
    dest = _dest(tmp_path)
    with _held_partial(dest) as live:
        _unlink_orphan_partials(dest)
        assert live.exists()
        assert live.read_bytes() == b"a live sibling is still writing this"


def test_sweep_unlinks_an_unheld_crash_orphan(tmp_path: Path) -> None:
    # The behaviour the flock gate must not cost: a killed worker leaves an unlocked partial and it
    # is still collected on the next fetch, rather than waiting for full investigation reclaim.
    dest = _dest(tmp_path)
    orphan = dest.parent / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked by a killed worker")

    _unlink_orphan_partials(dest)

    assert not orphan.exists()


def test_sweep_unlinks_only_the_orphan_when_a_live_partial_sits_beside_it(tmp_path: Path) -> None:
    # Both cases in one directory, because the interesting failure is a gate that is all-or-nothing
    # -- one that skips the whole sweep on the first locked candidate would leak the orphan, and one
    # that ignores the lock would destroy the live partial.
    dest = _dest(tmp_path)
    orphan = dest.parent / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked")

    with _held_partial(dest) as live:
        _unlink_orphan_partials(dest)
        assert live.exists()
    assert not orphan.exists()


def test_sweep_tolerates_a_candidate_that_vanishes_between_the_glob_and_the_open(
    tmp_path: Path,
) -> None:
    # The reclaim-side backstop sweeps the same directory, so a candidate can be gone by the time
    # this one opens it. That is the achieved post-state, not a fault: the sweep is best-effort and
    # runs holding the fetch lock, so it must never raise into the provision.
    dest = _dest(tmp_path)
    orphan = dest.parent / f"{_TOKEN}.deadbeef.partial"
    orphan.write_bytes(b"leaked")
    real_open = os.open

    def vanishing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == orphan:
            orphan.unlink()
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", vanishing_open)
        _unlink_orphan_partials(dest)

    assert not orphan.exists()


def _hold_flock_child(path: str, locked: Any) -> None:  # pragma: no cover - child process
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    locked.set()
    time.sleep(300)


def test_sweep_collects_a_partial_once_its_holder_process_dies(tmp_path: Path) -> None:
    # The property that makes flock beat an mtime window, proven against a real process rather than
    # asserted: the kernel drops the lock when the holding descriptor closes, and a SIGKILLed worker
    # closes every descriptor. So a crash orphan needs no timeout to age out -- it is collectable
    # the instant its holder is gone, while a live holder is protected for as long as it lives.
    dest = _dest(tmp_path)
    partial = dest.parent / f"{_TOKEN}.crashed.partial"
    partial.write_bytes(b"mid-download when the worker was killed")
    ctx = mp.get_context("spawn")
    locked = ctx.Event()
    proc = ctx.Process(target=_hold_flock_child, args=(str(partial), locked))
    proc.start()
    try:
        assert locked.wait(timeout=60), "the child never took the flock"
        _unlink_orphan_partials(dest)
        assert partial.exists(), "a live holder's partial was swept"
    finally:
        proc.kill()
        proc.join(timeout=60)
    assert proc.exitcode is not None, "the flock holder did not exit"

    _unlink_orphan_partials(dest)

    assert not partial.exists()


class _StallingReader:
    """Serves the object in two halves, blocking before the tail until the test releases it.

    The pause puts a *real* fetcher mid-download with its partial on disk and partly written, which
    is the only state in which the sweep's guard means anything.
    """

    def __init__(self, data: bytes, reached: threading.Event, release: threading.Event) -> None:
        self._chunks = [data[: len(data) // 2], data[len(data) // 2 :]]
        self._served = False
        self._reached = reached
        self._release = release

    def read(self, size: int = -1, /) -> bytes:
        if not self._chunks:
            return b""
        if self._served:
            self._reached.set()
            assert self._release.wait(timeout=60), "the staging thread was never released"
        self._served = True
        return self._chunks.pop(0)

    def close(self) -> None:
        return None


class _StallingStore:
    """An identity store whose body read stalls mid-object (see :class:`_StallingReader`)."""

    def __init__(self, data: bytes, reached: threading.Event, release: threading.Event) -> None:
        self._data = data
        self._reached = reached
        self._release = release

    def head(self, key: str) -> HeadResult:
        return HeadResult(
            size_bytes=len(self._data), checksum_sha256=_sha256_b64(self._data), etag="e"
        )

    @contextmanager
    def get_artifact_stream(self, key: str, etag: str | None) -> Iterator[StreamedArtifact]:
        reader = _StallingReader(self._data, self._reached, self._release)
        try:
            yield StreamedArtifact(cast(IO[bytes], reader), Sensitivity.SENSITIVE, "rootfs")
        finally:
            reader.close()

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._data[start : start + length]


def test_a_staging_fetchers_own_partial_survives_a_concurrent_sweep(tmp_path: Path) -> None:
    # End-to-end in the shape the bug takes: fetcher A is mid-download when the sweep runs, and the
    # sweep must leave A's partial alone and let A publish. This pins that the writer takes the lock
    # itself -- a guard applied only in the sweep's own tests would pass those and still lose here.
    payload = _QCOW2 + b"\xa5" * 4096
    reached, release = threading.Event(), threading.Event()
    store = _StallingStore(payload, reached, release)
    dest = _dest(tmp_path)
    failure: list[BaseException] = []

    def stage() -> None:
        try:
            stage_uploaded_rootfs(
                store,
                object_key="local/investigations/inv/rootfs-token",
                dest=dest,
                encoding=None,
                uncompressed_size=None,
                system_id=uuid4(),
            )
        except BaseException as err:  # noqa: BLE001 - re-raised in the main thread below
            failure.append(err)

    worker = threading.Thread(target=stage)
    worker.start()
    try:
        assert reached.wait(timeout=60), "the staging thread never reached the stall"
        partials = list(dest.parent.glob(f"{_TOKEN}.*.partial"))
        assert len(partials) == 1, f"expected one in-flight partial, found {partials}"
        _unlink_orphan_partials(dest)
        assert partials[0].exists(), "the sweep destroyed a live fetcher's own partial"
    finally:
        release.set()
        worker.join(timeout=60)

    assert not failure, f"the staging thread failed: {failure}"
    assert not worker.is_alive()
    assert dest.read_bytes() == payload  # published despite the concurrent sweep
    assert list(dest.parent.glob(f"{_TOKEN}.*.partial")) == []


def test_fetch_leaves_a_live_siblings_partial_and_still_publishes(tmp_path: Path) -> None:
    # #1524 acceptance at the call site: the sweep runs from inside fetch_uploaded_rootfs while it
    # holds the fetch lock, which is precisely where the lost-lock ordering puts it.
    inv = uuid4()
    dest = staged_rootfs_path(inv, _TOKEN, upload_dir=tmp_path)
    dest.parent.mkdir(parents=True)
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with _held_partial(dest) as live:
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]
        assert live.exists(), "the fetch swept a live sibling's partial out from under it"

    assert result == dest
    assert dest.read_bytes() == _QCOW2


def test_stage_fails_loud_when_its_partial_is_unlinked_in_the_create_lock_window(
    tmp_path: Path,
) -> None:
    # ADR-0446 §3: creating the partial and locking it are two syscalls, and a sweeper that wins the
    # gap would leave the fetcher streaming gigabytes into an inode no path reaches -- blocks
    # charged to df and invisible to every path-matching tool. The nlink check turns that into a
    # named failure that downloads nothing, rather than a silent leak.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    real_flock = fcntl.flock

    def sweeping_flock(fd: Any, operation: int) -> None:
        for partial in tmp_path.glob(f"{_TOKEN}.*.partial"):
            partial.unlink()
        real_flock(fd, operation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", sweeping_flock)
        with pytest.raises(CategorizedError) as error:
            _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "took the staging partial between its creation and its lock" in str(error.value)
    assert store.stream_calls == 0  # failed before spending the download
    assert not _dest(tmp_path).exists()


def test_sweep_warns_when_it_skips_a_partial_a_live_sibling_holds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The skip is correct and also the only externally visible symptom of the lost session lock: its
    # other consequence is a redundant multi-GiB download that reads as ordinary slowness. Silent,
    # the condition this change exists to survive would be undiagnosable.
    dest = _dest(tmp_path)
    with caplog.at_level(logging.WARNING), _held_partial(dest) as live:
        _unlink_orphan_partials(dest)

    assert live.exists()
    assert any(
        str(live) in r.getMessage() and "still holds it" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), caplog.text


def test_sweep_warns_and_skips_a_candidate_it_cannot_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A narrowing of the unconditional unlink this replaces, which needed only write+execute on the
    # DIRECTORY and no permission on the file -- so a partial that can be unlinked but not opened
    # was collected before and is not now. It is deliberate (a partial this process cannot open is
    # one it cannot show is dead, and unlinking it blind is the bug being fixed) and it must be
    # loud, because an ENOLCK-style filesystem would otherwise retire the sweep in total silence.
    dest = _dest(tmp_path)
    orphan = dest.parent / f"{_TOKEN}.unreadable.partial"
    orphan.write_bytes(b"leaked")
    real_open = os.open

    def denying_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == orphan:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", denying_open)
        _unlink_orphan_partials(dest)

    assert orphan.exists()  # left for the reclaim backstop, not unlinked unchecked
    assert any("could not open the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_stage_fails_loud_when_a_sweep_holds_the_lock_across_the_create_window(
    tmp_path: Path,
) -> None:
    # The second create-then-lock interleaving. If the sweep is still holding its own lock when the
    # writer tries, the writer gets EWOULDBLOCK before the st_nlink check can run -- which would
    # otherwise surface as "failed to stage ...: Resource temporarily unavailable" and point an
    # operator at the object store over a purely local race. Both interleavings must name the sweep.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    real_open = os.open
    held: list[int] = []

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith(".partial") and flags & os.O_CREAT:
            rival = real_open(path, os.O_RDONLY)  # the sweep, mid-unlink, still holding its lock
            fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(rival)
        return fd

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", racing_open)
            with pytest.raises(CategorizedError) as error:
                _stage(store, tmp_path, encoding=None, uncompressed_size=None)
    finally:
        for fd in held:
            os.close(fd)

    assert held, "the test never took the rival lock, so the window was not exercised"
    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "took the staging partial between its creation and its lock" in str(error.value)
    assert store.stream_calls == 0  # failed before spending the download


def test_the_partial_stays_locked_after_the_stagers_writer_closes(tmp_path: Path) -> None:
    # The guard is correct only because fcntl.flock is BSD flock(2), whose lock belongs to the open
    # file DESCRIPTION and so survives the stager's own "wb" handle being closed underneath it.
    # POSIX record locks (fcntl.lockf / F_SETLK) drop a process's locks when ANY descriptor on the
    # file closes, so a swap to the "more portable" API would silently unprotect exactly the
    # verify-and-publish window -- and every other test here would still pass. This one sweeps in
    # that window, after the stager returned and before the publish.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _dest(tmp_path)
    swept: list[list[Path]] = []
    real_require = rootfs_upload_fetch._require_qcow2_magic

    def sweeping_require(staged: Path, *, system_id: str) -> None:
        _unlink_orphan_partials(dest)  # a sibling sweeps between the writer close and the publish
        swept.append(list(dest.parent.glob(f"{_TOKEN}.*.partial")))
        real_require(staged, system_id=system_id)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rootfs_upload_fetch, "_require_qcow2_magic", sweeping_require)
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert swept and swept[0], "the sweep unlinked the partial after the stager closed its writer"
    assert dest.read_bytes() == _QCOW2


def test_stage_keeps_a_base_a_sibling_published_during_the_download(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The far end of the same lost-lock scenario. Now that the losing fetcher survives its sweep, it
    # reaches the publish -- and the sibling that held the lock has by then normally published dest
    # and had a guest's overlay created against it. The bytes are identical (content-addressed), so
    # replacing it is not corruption; it would orphan an inode of up to the 50 GiB cap behind that
    # guest's open descriptor, re-creating the invisible-blocks symptom this change removes.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _dest(tmp_path)
    dest.write_bytes(_QCOW2)
    published_inode = dest.stat().st_ino

    with caplog.at_level(logging.WARNING):
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.stat().st_ino == published_inode, "the sibling's published base was replaced"
    assert dest.read_bytes() == _QCOW2
    assert list(dest.parent.glob(f"{_TOKEN}.*.partial")) == []  # our copy discarded
    assert any("a sibling published" in r.getMessage() for r in caplog.records), caplog.text


def test_stage_publishes_over_a_torn_base_a_sibling_left(tmp_path: Path) -> None:
    # The complement, and the reason the guard is a format probe rather than a bare exists(): a
    # present dest that does not pass the qcow2 gate is not a base worth keeping, so the freshly
    # verified download must still publish over it (ADR-0443's torn-base shape).
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _dest(tmp_path)
    dest.write_bytes(b"\x00" * len(_QCOW2))
    torn_inode = dest.stat().st_ino

    _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    assert dest.stat().st_ino != torn_inode


def test_stage_publishes_when_the_published_base_check_cannot_read_dest(tmp_path: Path) -> None:
    # Polarity is the opposite of _reusable_staged_base's, which raises on an unreadable dest to
    # avoid a silent perpetual re-download. Here a False costs one os.replace -- which REPAIRS
    # an unreadable dest, a rename needing permission on the directory rather than the file --
    # so an OSError must never turn a download that already succeeded into a failure. The trade
    # is deliberate: skipping would hand back a base this process could not evaluate at all,
    # which is worse than the inode orphan it avoids when a verified copy of the same
    # content-addressed bytes is in hand (ADR-0446 section 7 records the residue).
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    dest = _dest(tmp_path)
    dest.write_bytes(_QCOW2)
    real_stat = Path.stat

    def faulting_stat(self: Path, **kwargs: Any) -> Any:
        if self == dest:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_stat(self, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "stat", faulting_stat)
        _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    assert list(dest.parent.glob(f"{_TOKEN}.*.partial")) == []


def test_fetch_survives_a_dead_session_when_releasing_the_lock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The flock guard saves the fetcher from the sweep, and then the `finally` released the lock on
    # the very connection whose loss is the whole premise -- raising, and failing the provision one
    # line later. A `finally` also REPLACES an in-flight exception, so an actionable
    # CategorizedError would reach the operator as a bare Postgres error. A terminated backend
    # has already
    # released the lock, so suppressing is the correct semantics, not a convenience.
    inv = uuid4()
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    real_execute = conn.execute

    def dying_execute(sql: str, params: Any = None) -> Any:
        if "pg_advisory_unlock" in sql:
            conn.closed = True  # libpq marks a terminated connection BAD before the raise
            raise psycopg.OperationalError("terminating connection due to administrator command")
        return real_execute(sql, params)

    conn.execute = dying_execute  # ty: ignore[invalid-assignment]
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with caplog.at_level(logging.WARNING):
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == _QCOW2  # published, not a failed provision
    assert any(
        "could not release the rootfs fetch advisory lock" in r.getMessage() for r in caplog.records
    ), caplog.text


def test_a_dead_session_does_not_mask_the_real_staging_error(tmp_path: Path) -> None:
    # The second-order half: the unlock ran in a `finally`, so on a connection that had since died
    # it replaced whatever the fetch was already raising. A checksum mismatch has to stay a checksum
    # mismatch, not become an admin-shutdown message with the actionable text in __context__.
    inv = uuid4()
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    real_execute = conn.execute

    def dying_execute(sql: str, params: Any = None) -> Any:
        if "pg_advisory_unlock" in sql:
            conn.closed = True  # libpq marks a terminated connection BAD before the raise
            raise psycopg.OperationalError("terminating connection due to administrator command")
        return real_execute(sql, params)

    conn.execute = dying_execute  # ty: ignore[invalid-assignment]
    store = _FakeStore(b"actual-bytes", checksum=_sha256_b64(b"different-bytes"))

    with pytest.raises(CategorizedError) as error:
        fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "checksum verification" in str(error.value)


def test_stage_completes_unguarded_where_the_filesystem_cannot_flock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ENOLCK on an NFS mount whose lock manager is down, EOPNOTSUPP on some FUSE and 9p backends.
    # Staging unguarded is exactly the pre-ADR-0446 behaviour and no worse -- a sweep on that same
    # filesystem cannot lock either, so it skips every candidate. Failing instead would turn a
    # filesystem without lock support into a total uploaded-rootfs outage.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    def unlockable_flock(fd: Any, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", unlockable_flock)
        dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert dest.read_bytes() == _QCOW2
    assert any("cannot flock the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_sweep_warns_and_continues_when_one_candidate_cannot_be_unlinked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The fourth sweep outcome, and the likeliest to matter: EPERM under a sticky-bit or foreign-uid
    # staging directory -- the ADR-0442 ownership asymmetry this subsystem was already bitten by.
    # Left to escape, it landed in the caller's suppress() around the whole loop, so it emitted
    # nothing AND abandoned every remaining orphan of that base on that pass.
    dest = _dest(tmp_path)
    stuck = dest.parent / f"{_TOKEN}.0000stuck.partial"
    collectable = dest.parent / f"{_TOKEN}.9999free.partial"
    for path in (stuck, collectable):
        path.write_bytes(b"leaked")
    real_unlink = Path.unlink

    def sticky_unlink(self: Path, **kwargs: Any) -> None:
        if self == stuck:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        real_unlink(self, **kwargs)

    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "unlink", sticky_unlink)
        _unlink_orphan_partials(dest)

    assert stuck.exists()
    assert not collectable.exists(), "one unsweepable candidate truncated the rest of the pass"
    assert any("could not unlink the staging partial" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_the_published_base_stays_readable_by_the_hypervisor(tmp_path: Path) -> None:
    # The partial's mode is carried onto the published base by os.replace, and QEMU reads that base
    # as the unprivileged hypervisor user -- so the explicit 0o666 in _flocked_partial is NOT
    # free to tighten to the 0o600 a SENSITIVE file invites. Nothing else in this suite would
    # notice: every other test asserts bytes, and a 0o600 base fails only at guest boot on a real
    # host. Same canary role as the lockf test above.
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))
    previous = os.umask(0o022)
    try:
        dest = _stage(store, tmp_path, encoding=None, uncompressed_size=None)
        mode = stat.S_IMODE(dest.stat().st_mode)
    finally:
        os.umask(previous)

    assert mode == 0o666 & ~0o022, f"the published base was staged {mode:o}, not 0o644"
    assert mode & (stat.S_IRGRP | stat.S_IROTH), "the hypervisor user cannot read the staged base"


def test_a_failed_unlock_on_a_live_session_does_not_claim_the_lock_was_released(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # `except psycopg.Error` is wider than the two triggers ADR-0446 names. A role or database
    # statement_timeout cancels the unlock as QueryCanceled on a session that is still open and
    # STILL HOLDING the lock -- so the "its session is gone, which already released it" line would
    # affirmatively deny the very condition an operator is staring at, while every sibling fetch of
    # this base blocks. Suppressing stays right; narrating an unobserved cause does not.
    inv = uuid4()
    conn = _FakeConn(investigation_id=inv, owned_object_key=_owned_key(inv))
    conn.closed = False
    real_execute = conn.execute

    def timing_out_execute(sql: str, params: Any = None) -> Any:
        if "pg_advisory_unlock" in sql:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return real_execute(sql, params)

    conn.execute = timing_out_execute  # ty: ignore[invalid-assignment]
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    with caplog.at_level(logging.WARNING):
        result = fetch_uploaded_rootfs(conn, store, _upload(tmp_path))  # ty: ignore[invalid-argument-type]

    assert result.read_bytes() == _QCOW2  # still not a failed provision
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "still open" in logged and "may still be held" in logged
    assert "already released it" not in logged


def test_stage_names_the_sweep_when_the_unguarded_partial_is_taken_mid_download(
    tmp_path: Path,
) -> None:
    # On the ENOLCK degrade path nothing keeps a sweeper out for the whole transfer: ADR-0446 5's
    # symmetry argument ("a sweeper there cannot lock either") holds at one instant, not across
    # minutes, and a recovering lockd falsifies it mid-download. The outcome is then the
    # pre-ADR-0446 one, which is the point of degrading rather than failing -- but the diagnosis
    # must not be, because a bare ENOENT at os.replace reads as an object-store fault.
    dest = _dest(tmp_path)
    store = _FakeStore(_QCOW2, checksum=_sha256_b64(_QCOW2))

    def unlockable_flock(fd: Any, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    real_stream = store.get_artifact_stream

    @contextmanager
    def sweeping_stream(key: str, etag: str | None) -> Iterator[StreamedArtifact]:
        with real_stream(key, etag) as fetched:
            # A sibling sweeps once the lock manager is back, while this download is still running.
            for partial in dest.parent.glob(f"{_TOKEN}.*.partial"):
                partial.unlink()
            yield fetched

    store.get_artifact_stream = sweeping_stream  # ty: ignore[invalid-assignment]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", unlockable_flock)
        with pytest.raises(CategorizedError) as error:
            _stage(store, tmp_path, encoding=None, uncompressed_size=None)

    assert error.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # Named for the window it actually went in. "between its creation and its lock" would point the
    # operator at a sub-millisecond race that additionally needs a lost session lock, and away from
    # the two conditions that really produce this -- which is most of what the check is here for.
    assert "concurrent orphan sweep" in str(error.value)
    assert "while it was being downloaded" in str(error.value)
    assert "#1544" in str(error.value)
    assert not dest.exists()
