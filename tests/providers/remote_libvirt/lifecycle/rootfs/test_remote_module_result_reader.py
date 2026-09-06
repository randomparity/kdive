"""Sparse, bounded reads of the remote module result document."""

from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs import remote_module_result_reader
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    CompletionDeadlineExecutor,
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_result_reader import (
    SparseRemoteModuleResultReader,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    SCRATCH_CAPACITY_BYTES,
    PreparedVolume,
    StorageConn,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Clock,
    DeadlineAwareExecutor,
    ThreadDeadlineExecutor,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents_support import (
    NONCE,
    RUN_ID,
    SYSTEM_ID,
)

SCRATCH_NAME = render_module_volume_name(SYSTEM_ID, RUN_ID, NONCE, "scratch.ext4")


class _Stream:
    def __init__(self, events: list[bytes | int]) -> None:
        self.events = events
        self.sparse_called = False
        self.dense_called = False
        self.finished = False
        self.aborted = False
        self.finish_error: BaseException | None = None

    def sparseRecvAll(self, data, hole, opaque) -> None:  # noqa: N802, ANN001
        self.sparse_called = True
        for event in self.events:
            if isinstance(event, bytes):
                data(self, event, opaque)
            else:
                hole(self, event, opaque)

    def recvAll(self, handler, opaque) -> None:  # noqa: N802, ANN001
        del handler, opaque
        self.dense_called = True
        raise AssertionError("dense receive must not be used")

    def finish(self) -> int:
        self.finished = True
        if self.finish_error is not None:
            raise self.finish_error
        return 0

    def abort(self) -> int:
        self.aborted = True
        return 0


class _Volume:
    def __init__(self, stream: _Stream) -> None:
        self.stream = stream
        self.download_flags: int | None = None
        self.expire: Callable[[], None] | None = None

    def info(self) -> tuple[int, int, int]:
        return (0, SCRATCH_CAPACITY_BYTES, 8192)

    def download(self, stream: object, _offset: int, _length: int, flags: int) -> int:
        assert stream is self.stream
        self.download_flags = flags
        if self.expire is not None:
            self.expire()
        return 0


class _Pool:
    def __init__(self, volume: _Volume) -> None:
        self.volume = volume

    def storageVolLookupByName(self, name: str) -> _Volume:  # noqa: N802
        assert name == SCRATCH_NAME
        return self.volume


class _Storage:
    def __init__(self, stream: _Stream) -> None:
        self.stream = stream
        self.pool = _Pool(_Volume(stream))
        self.fail_new_stream = False
        self.after_new_stream: Callable[[], None] | None = None

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        assert name == "pool"
        return self.pool

    def newStream(self, _flags: int = 0) -> _Stream:  # noqa: N802
        if self.fail_new_stream:
            raise OSError("stream unavailable")
        if self.after_new_stream is not None:
            self.after_new_stream()
        return self.stream


def _scratch() -> PreparedVolume:
    return PreparedVolume(
        "pool",
        SCRATCH_NAME,
        SYSTEM_ID,
        RUN_ID,
        NONCE,
        "scratch",
        "sha256:" + "0" * 64,
        SCRATCH_CAPACITY_BYTES,
    )


def test_sparse_reader_requests_sparse_stream_and_preserves_holes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _Stream([b"x" * 4096, SCRATCH_CAPACITY_BYTES - 4096])
    storage = _Storage(stream)
    clock = Clock()
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(clock),
        monotonic=clock,
    )
    monkeypatch.setattr(remote_module_result_reader, "_read_debugfs", lambda *_args: b"")

    assert reader.read_volume(_scratch()) == b""

    assert storage.pool.volume.download_flags is not None
    assert stream.sparse_called
    assert not stream.dense_called
    assert stream.finished


class _ImageStream:
    def __init__(self, image: Path, capacity: int) -> None:
        self.image = image
        self.capacity = capacity
        self.sparse_called = False
        self.finished = False

    def sparseRecvAll(self, data, hole, opaque) -> None:  # noqa: N802, ANN001
        self.sparse_called = True
        descriptor = os.open(self.image, os.O_RDONLY)
        try:
            offset = 0
            while offset < self.capacity:
                try:
                    data_offset = os.lseek(descriptor, offset, os.SEEK_DATA)
                except OSError as exc:
                    assert exc.errno == errno.ENXIO
                    hole(self, self.capacity - offset, opaque)
                    return
                if data_offset > offset:
                    hole(self, data_offset - offset, opaque)
                    offset = data_offset
                hole_offset = os.lseek(descriptor, offset, os.SEEK_HOLE)
                while offset < hole_offset:
                    chunk = os.pread(descriptor, min(64 * 1024, hole_offset - offset), offset)
                    assert chunk
                    data(self, chunk, opaque)
                    offset += len(chunk)
        finally:
            os.close(descriptor)

    def finish(self) -> int:
        self.finished = True
        return 0

    def abort(self) -> int:
        return 0


class _ImageVolume:
    def __init__(self, image: Path, stream: _ImageStream) -> None:
        self.image = image
        self.stream = stream
        self.flags: int | None = None

    def info(self) -> tuple[int, int, int]:
        stat = self.image.stat()
        return (0, stat.st_size, stat.st_blocks * 512)

    def download(self, stream: object, _offset: int, _length: int, flags: int) -> int:
        assert stream is self.stream
        self.flags = flags
        return 0


class _ImageStorage:
    def __init__(self, volume: _ImageVolume, stream: _ImageStream) -> None:
        self.volume = volume
        self.stream = stream

    def storagePoolLookupByName(self, name: str) -> _ImageStorage:  # noqa: N802
        assert name == "pool"
        return self

    def storageVolLookupByName(self, name: str) -> _ImageVolume:  # noqa: N802
        assert name == SCRATCH_NAME
        return self.volume

    def newStream(self, _flags: int = 0) -> _ImageStream:  # noqa: N802
        return self.stream


def test_sparse_reader_extracts_real_ext4_without_dense_local_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "source-scratch.ext4"
    with image.open("wb") as handle:
        handle.truncate(SCRATCH_CAPACITY_BYTES)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", str(image)], check=True, capture_output=True, timeout=120
    )
    result = b'{"result":"sparse"}\n'
    payload = tmp_path / "result-v1.json"
    payload.write_bytes(result)
    subprocess.run(
        ["debugfs", "-w", "-R", f"write {payload} /result-v1.json", str(image)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    source_allocation = image.stat().st_blocks * 512
    assert source_allocation < SCRATCH_CAPACITY_BYTES // 8

    stream = _ImageStream(image, SCRATCH_CAPACITY_BYTES)
    storage = _ImageStorage(_ImageVolume(image, stream), stream)
    observed: dict[str, int] = {}
    original = remote_module_result_reader._read_debugfs

    def inspect_receiver(path: Path, deadline: float, monotonic) -> bytes | None:  # noqa: ANN001
        stat = path.stat()
        observed.update(size=stat.st_size, allocation=stat.st_blocks * 512)
        return original(path, deadline, monotonic)

    monkeypatch.setattr(remote_module_result_reader, "_read_debugfs", inspect_receiver)
    clock = Clock()
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(clock),
        monotonic=clock,
    )

    assert reader.read_volume(_scratch()) == result
    assert stream.sparse_called and stream.finished
    assert storage.volume.flags == libvirt.VIR_STORAGE_VOL_DOWNLOAD_SPARSE_STREAM
    assert observed["size"] == SCRATCH_CAPACITY_BYTES
    assert observed["allocation"] < SCRATCH_CAPACITY_BYTES // 8
    assert observed["allocation"] <= source_allocation + 4 * 1024 * 1024


def test_debugfs_returns_none_only_for_a_real_absent_result(tmp_path: Path) -> None:
    image = tmp_path / "empty-scratch.ext4"
    with image.open("wb") as handle:
        handle.truncate(SCRATCH_CAPACITY_BYTES)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", str(image)], check=True, capture_output=True, timeout=120
    )

    assert remote_module_result_reader._read_debugfs(image, 10.0, lambda: 0.0) is None


def test_debugfs_rejects_a_malformed_filesystem(tmp_path: Path) -> None:
    image = tmp_path / "malformed.ext4"
    image.write_bytes(b"not an ext4 filesystem")

    with pytest.raises(CategorizedError) as exc_info:
        remote_module_result_reader._read_debugfs(image, 10.0, lambda: 0.0)

    assert exc_info.value.category is ErrorCategory.CONFLICT


def test_invalid_utf8_debugfs_stderr_with_exit_zero_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_popen = subprocess.Popen

    def invalid_stderr_process(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return original_popen(
            ["sh", "-c", "printf '\\377' >&2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(remote_module_result_reader.subprocess, "Popen", invalid_stderr_process)

    with pytest.raises(CategorizedError) as exc_info:
        remote_module_result_reader._read_debugfs(tmp_path / "unused", 10.0, lambda: 0.0)

    assert exc_info.value.category is ErrorCategory.CONFLICT


def test_new_stream_failure_closes_mkstemp_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor, raw_path = tempfile.mkstemp(dir=tmp_path)
    storage = _Storage(_Stream([]))
    storage.fail_new_stream = True
    monkeypatch.setattr(
        remote_module_result_reader.tempfile,
        "mkstemp",
        lambda **_kwargs: (descriptor, raw_path),
    )
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(Clock()),
    )

    with pytest.raises(CategorizedError):
        reader.read_volume(_scratch())

    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not Path(raw_path).exists()


def test_sparse_reader_does_not_start_receive_after_deadline(tmp_path: Path) -> None:
    stream = _Stream([])
    storage = _Storage(stream)
    clock = Clock()
    storage.pool.volume.expire = lambda: setattr(clock, "value", 1.0)
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(clock),
        monotonic=clock,
    )

    with pytest.raises(CategorizedError) as exc_info:
        reader.read_volume(_scratch(), deadline=1.0)

    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not stream.sparse_called
    assert not stream.finished
    assert stream.aborted


def test_sparse_reader_aborts_after_deadline_during_finish(tmp_path: Path) -> None:
    stream = _Stream([SCRATCH_CAPACITY_BYTES])
    stream.finish_error = TimeoutError()
    storage = _Storage(stream)
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(Clock()),
    )

    with pytest.raises(CategorizedError) as exc_info:
        reader.read_volume(_scratch())

    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert stream.aborted


def test_postcompletion_new_stream_timeout_aborts_and_closes_descriptor(tmp_path: Path) -> None:
    stream = _Stream([])
    storage = _Storage(stream)
    clock = Clock()

    storage.after_new_stream = lambda: setattr(clock, "value", 1.0)
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=CompletionDeadlineExecutor(clock),
        monotonic=clock,
    )

    with pytest.raises(CategorizedError) as exc_info:
        reader.read_volume(_scratch(), deadline=1.0)

    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert stream.aborted
    assert not list(tmp_path.glob("kdive-module-result-*.ext4"))


def test_sparse_reader_refuses_foreign_closed_scratch_identity(tmp_path: Path) -> None:
    stream = _Stream([])
    storage = _Storage(stream)
    reader = SparseRemoteModuleResultReader(
        storage=cast(StorageConn, storage),
        work_dir=tmp_path,
        executor=DeadlineAwareExecutor(Clock()),
    )

    with pytest.raises(CategorizedError) as exc_info:
        reader.read_volume(replace(_scratch(), digest="sha256:" + "f" * 64))

    assert exc_info.value.category is ErrorCategory.CONFLICT
    assert storage.pool.volume.download_flags is None


class _BlockingSparseStream(_Stream):
    def __init__(self, release: threading.Event) -> None:
        super().__init__([])
        self.release = release
        self.entered = threading.Event()

    def sparseRecvAll(self, data, hole, opaque) -> None:  # noqa: N802, ANN001
        self.sparse_called = True
        self.entered.set()
        self.release.wait()
        hole(self, SCRATCH_CAPACITY_BYTES, opaque)


def _recovery() -> RemoteModuleRecoveryRefV1:
    digest = "sha256:" + "a" * 64
    return RemoteModuleRecoveryRefV1.model_validate(
        {
            "system_id": SYSTEM_ID,
            "run_id": RUN_ID,
            "plan_identity": digest,
            "operation_nonce": NONCE,
            "pool": {"ref": "pool"},
            "root_volume": {"ref": "root"},
            "source_volume": {"ref": "source"},
            "scratch_volume": {"ref": SCRATCH_NAME},
            "operation_identity": digest,
            "result_identity": digest,
            "appliance_image_digest": digest,
            "authority_identity": digest,
        }
    )


def test_cancelled_async_recovery_read_keeps_resources_until_stream_returns(tmp_path: Path) -> None:
    async def exercise() -> None:
        release = threading.Event()
        stream = _BlockingSparseStream(release)
        storage = _Storage(stream)
        executor = RemoteModulePreparationExecutor()
        reader = SparseRemoteModuleResultReader(
            storage=cast(StorageConn, storage),
            work_dir=tmp_path,
            executor=ThreadDeadlineExecutor(),
            preparation_executor=executor,
        )
        task = asyncio.create_task(reader.read_recovery_async(_recovery()))
        while not stream.entered.wait(0.01):
            await asyncio.sleep(0)
        task.cancel()
        task.cancel()
        assert not task.done()
        assert list(tmp_path.glob("kdive-module-result-*.ext4"))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(tmp_path.glob("kdive-module-result-*.ext4"))
        executor.shutdown()

    asyncio.run(exercise())
