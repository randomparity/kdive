"""Sparse, bounded extraction of a remote module scratch result."""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    DeadlineExecutor,
    UnresolvedCallError,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    parse_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    SCRATCH_CAPACITY_BYTES,
    PreparedVolume,
    StorageConn,
)

_RESULT_PATH = "/result-v1.json"
_RESULT_DOCUMENT_MAX_BYTES = 65_536
_INVOCATION_TIMEOUT_SECONDS = 5 * 60.0


def _conflict(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT)


def _operational(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.INFRASTRUCTURE_FAILURE)


def _pool_name(reference: str) -> str:
    name = reference.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise _conflict("remote module pool reference is invalid")
    return name


def _result_volume(recovery: RemoteModuleRecoveryRefV1) -> PreparedVolume:
    name = recovery.scratch_volume.ref.rsplit("/", 1)[-1]
    owner = parse_module_volume_name(name)
    if (
        owner is None
        or owner.kind != "scratch.ext4"
        or (
            owner.system_id,
            owner.run_id,
            owner.operation_nonce,
        )
        != (recovery.system_id, recovery.run_id, recovery.operation_nonce)
    ):
        raise _conflict("remote module scratch volume ownership differs from recovery reference")
    return PreparedVolume(
        _pool_name(recovery.pool.ref),
        name,
        recovery.system_id,
        recovery.run_id,
        recovery.operation_nonce,
        "scratch",
        "sha256:" + "0" * 64,
        SCRATCH_CAPACITY_BYTES,
    )


def _debugfs_diagnostics(error: bytes) -> list[str] | None:
    try:
        lines = [line for line in error.decode("utf-8", "strict").splitlines() if line]
    except UnicodeDecodeError:
        return None
    banner = re.compile(r"debugfs \d+\.\d+(?:\.\d+)? \([^)]+\)\Z")
    diagnostics = []
    for line in lines:
        if banner.fullmatch(line):
            continue
        normalized = line.removeprefix("debugfs:").strip()
        if normalized == f"cat {_RESULT_PATH}":
            continue
        diagnostics.append(normalized)
    return diagnostics


def _absent_debugfs_error(error: bytes) -> bool:
    return _debugfs_diagnostics(error) == [f"{_RESULT_PATH}: File not found by ext2_lookup"]


def _read_debugfs(image: Path, deadline: float, monotonic: Callable[[], float]) -> bytes | None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("remote module result deadline expired")
    process = subprocess.Popen(  # noqa: S603 - fixed tool and closed argument vector
        ["debugfs", "-R", f"cat {_RESULT_PATH}", str(image)],  # noqa: S607
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    error = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, (output, _RESULT_DOCUMENT_MAX_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, (error, 4096))
    try:
        while selector.get_map():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("remote module result deadline expired")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("remote module result deadline expired")
            for key, _ in events:
                target, limit = cast(tuple[bytearray, int], key.data)
                chunk = os.read(key.fd, min(8192, limit + 1 - len(target)))
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    target.extend(chunk)
                    if len(target) > limit:
                        raise ValueError("debugfs output exceeds provider limit")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("remote module result deadline expired")
        code = process.wait(timeout=remaining)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            pipe.close()
    if _absent_debugfs_error(bytes(error)):
        return None
    if code != 0 or _debugfs_diagnostics(bytes(error)) != []:
        raise _conflict("remote module durable filesystem is unreadable")
    return bytes(output)


@dataclass(frozen=True, slots=True)
class SparseRemoteModuleResultReader:
    """Read one fixed scratch document without materializing its holes."""

    storage: StorageConn
    work_dir: Path
    executor: DeadlineExecutor
    monotonic: Callable[[], float] = time.monotonic
    preparation_executor: RemoteModulePreparationExecutor | None = None

    def _call(
        self, operation: Callable[[], Any], deadline: float, *, require_admission: bool = True
    ) -> Any:
        completed = threading.Event()

        def admitted() -> Any:
            try:
                if require_admission and self.monotonic() >= deadline:
                    raise TimeoutError("remote module result deadline expired")
                return operation()
            finally:
                completed.set()

        try:
            return self.executor.call(admitted, deadline)
        except UnresolvedCallError:
            # The deadline executor has stopped waiting, not stopped libvirt.  Keep the file,
            # stream, and bounded capacity owned by this worker until its call actually returns.
            completed.wait()
            raise

    def _abort(self, stream: object, deadline: float) -> None:
        with contextlib.suppress(Exception):
            self._call(
                cast(Any, stream).abort,
                max(deadline, self.monotonic() + 1),
                require_admission=False,
            )

    def read_recovery(
        self, recovery: RemoteModuleRecoveryRefV1, *, deadline: float | None = None
    ) -> bytes | None:
        return self.read_volume(_result_volume(recovery), deadline=deadline)

    async def read_recovery_async(
        self, recovery: RemoteModuleRecoveryRefV1, *, deadline: float | None = None
    ) -> bytes | None:
        if self.preparation_executor is None:
            raise CategorizedError(
                "remote module result reader is not configured for async recovery reads",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return await self.preparation_executor.run(
            lambda: self.read_recovery(recovery, deadline=deadline)
        )

    def read_volume(
        self, scratch: PreparedVolume, *, deadline: float | None = None
    ) -> bytes | None:
        owner = parse_module_volume_name(scratch.name)
        if (
            scratch.purpose != "scratch"
            or scratch.digest != "sha256:" + "0" * 64
            or scratch.capacity_bytes != SCRATCH_CAPACITY_BYTES
            or owner is None
            or owner.kind != "scratch.ext4"
            or (owner.system_id, owner.run_id, owner.operation_nonce)
            != (scratch.system_id, scratch.run_id, scratch.operation_nonce)
        ):
            raise _conflict("remote module scratch volume ownership is invalid")
        limit = deadline if deadline is not None else self.monotonic() + _INVOCATION_TIMEOUT_SECONDS
        stream: object | None = None
        image: Path | None = None
        finished = False
        try:
            pool = self._call(lambda: self.storage.storagePoolLookupByName(scratch.pool), limit)
            try:
                volume = self._call(lambda: pool.storageVolLookupByName(scratch.name), limit)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                    return None
                raise
            info = self._call(volume.info, limit)
            capacity, allocation = int(info[1]), int(info[2])
            if capacity != scratch.capacity_bytes or allocation < 0 or allocation > capacity:
                raise _conflict("remote module scratch volume size is invalid")
            descriptor, raw_path = tempfile.mkstemp(
                prefix="kdive-module-result-", suffix=".ext4", dir=self.work_dir
            )
            image = Path(raw_path)
            logical = 0
            with os.fdopen(descriptor, "wb") as handle:

                def open_stream() -> object:
                    nonlocal stream
                    stream = self.storage.newStream(0)
                    return stream

                self._call(open_stream, limit)
                assert stream is not None

                def data(_stream: object, chunk: bytes, _opaque: object) -> int:
                    nonlocal logical
                    if not isinstance(chunk, bytes) or logical + len(chunk) > capacity:
                        raise ValueError("sparse download exceeds scratch capacity")
                    logical += len(chunk)
                    return handle.write(chunk)

                def hole(_stream: object, length: int, _opaque: object) -> None:
                    nonlocal logical
                    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                        raise ValueError("sparse download hole is invalid")
                    if logical + length > capacity:
                        raise ValueError("sparse download exceeds scratch capacity")
                    logical += length
                    handle.seek(length, os.SEEK_CUR)

                self._call(
                    lambda: volume.download(
                        stream, 0, capacity, libvirt.VIR_STORAGE_VOL_DOWNLOAD_SPARSE_STREAM
                    ),
                    limit,
                )
                self._call(lambda: cast(Any, stream).sparseRecvAll(data, hole, None), limit)
                self._call(cast(Any, stream).finish, limit)
                finished = True
                handle.truncate(capacity)
            return _read_debugfs(image, limit, self.monotonic)
        except CategorizedError:
            raise
        except TimeoutError as exc:
            if stream is not None and not finished:
                self._abort(stream, limit)
            raise _operational("remote module durable result read timed out") from exc
        except (
            AttributeError,
            OSError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            libvirt.libvirtError,
        ) as exc:
            if stream is not None and not finished:
                self._abort(stream, limit)
            raise _conflict("remote module durable result read failed") from exc
        finally:
            if image is not None:
                image.unlink(missing_ok=True)
