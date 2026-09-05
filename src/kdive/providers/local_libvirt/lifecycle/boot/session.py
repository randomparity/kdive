"""Operation-scoped local-libvirt external-boot host capability (ADRs 0587, 0600)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import selectors
import stat
import tempfile
import threading
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - serialization follows a defused parse
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol
from uuid import UUID, uuid4

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    RunningKernelObservation,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from kdive.providers.shared.runtime_paths import domain_name_for


@dataclass(frozen=True)
class OverlayIdentity:
    """Stable identity of the expected System overlay."""

    device: int
    inode: int


@dataclass(frozen=True)
class _BoundOverlay:
    device: int
    inode: int
    path: str
    descriptor: int


@dataclass(frozen=True)
class ClosedDomainInspection:
    """Immutable facts derived from one persistent/inactive domain definition."""

    xml: bytes
    active: bool
    definition_identity: str
    source_boot_identity: str
    domain_name: str
    overlay: OverlayIdentity


class LocalExternalBootOperationLease(Protocol):
    """Opaque capability issued by the owner of the live System lane."""

    system_id: UUID
    binding: ExternalBootActivationBinding


class LocalExternalBootOperationPin(Protocol):
    """Retained proof that the issuing lane remains held."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class OperationOwnership:
    """Immutable pin-free ownership facts safe for descriptor policy callbacks."""

    system_id: UUID
    binding: ExternalBootActivationBinding


@dataclass(frozen=True)
class ExpectedOperationOwnership:
    """Caller-owned identity required before the session opens resources."""

    system_id: UUID
    run_id: UUID
    activation_id: UUID | None


@dataclass(frozen=True)
class PinnedOperationOwnership:
    """Atomic lane result whose pin remains inside the session factory."""

    ownership: OperationOwnership
    _pin: LocalExternalBootOperationPin


type PinOperationLease = Callable[[LocalExternalBootOperationLease], PinnedOperationOwnership]
type OpenArtifactRoot = Callable[[OperationOwnership], int]


class RunningDomain(Protocol):
    def name(self) -> str: ...
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802


class _Domain(RunningDomain, Protocol):
    def isActive(self) -> int: ...  # noqa: N802
    def destroy(self) -> int: ...
    def create(self) -> int: ...
    def free(self) -> object: ...


class _Connection(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802
    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802
    def close(self) -> object: ...


type Connect = Callable[[], _Connection]


class _Guest(Protocol):
    def add_drive_opts(self, overlay: str, *, format: str) -> None: ...
    def launch(self) -> None: ...
    def inspect_os(self) -> list[str]: ...
    def mount(self, device: str, mountpoint: str) -> None: ...
    def shutdown(self) -> None: ...
    def close(self) -> None: ...
    def find0(self, directory: str, files: str) -> None: ...
    def last_errno(self) -> int: ...
    def user_cancel(self) -> None: ...

    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def download(self, remotefilename: str, filename: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def mv(self, source: str, destination: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def sync(self) -> None: ...


type OpenGuest = Callable[[], _Guest]
type ReadinessProbe = Callable[[UUID], ReadinessResult]
type RunningObserver = Callable[[UUID, RunningDomain], RunningKernelObservation]
type CleanupPayloads = Callable[[int, ExternalBootActivationBinding], None]


@dataclass(frozen=True, slots=True)
class InactiveGuestDirectoryEntry:
    """One validated relative name from a bounded recursive guest walk."""

    path: str


class TreeCursor(AbstractContextManager[Iterator[InactiveGuestDirectoryEntry]], Protocol):
    """Cancellable, operation-owned cursor over one recursive guest tree."""

    def close(self) -> None: ...


class LocalExternalBootSession(Protocol):
    def inspect_closed(self) -> ClosedDomainInspection: ...
    def require_inactive(self) -> None: ...
    def stop_and_require_inactive(self) -> None: ...
    def open_artifact(self, name: str, flags: int, mode: int = 0o600) -> int: ...
    def unlink_artifact(self, name: str) -> None: ...
    def guest(self) -> AbstractContextManager[InactiveGuest]: ...
    def define_xml(self, xml: str) -> None: ...
    def start(self) -> None: ...
    def readiness(self) -> ReadinessResult: ...
    def observe_running(self) -> RunningKernelObservation: ...
    def restore_power(self, prior: Literal["running", "inactive"]) -> None: ...
    def cleanup_payloads(self) -> None: ...
    def close(self) -> None: ...


class InactiveGuest(Protocol):
    """The session's deliberately small libguestfs capability."""

    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def open_tree(self, path: str, *, limit: int) -> TreeCursor: ...
    def open_regular(self, path: str, *, size: int) -> AbstractContextManager[BinaryIO]: ...
    def create_regular(self, content: BinaryIO, path: str, *, size: int) -> None: ...
    def download_artifact(self, guest_source: str, artifact_name: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def upload_artifact(self, artifact_name: str, guest_destination: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def mv(self, source: str, destination: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def sync(self) -> None: ...


class _GuestContext(AbstractContextManager[InactiveGuest]):
    def __init__(self, session: _ConcreteSession) -> None:
        self._session = session
        self._guest: _Guest | None = None
        self._cursors: set[_Find0TreeCursor] = set()
        self._closed = False

    def __enter__(self) -> InactiveGuest:
        with self._session._lifecycle_lock:
            if self._closed:
                raise RuntimeError("guest wrapper is closed")
            self._guest = self._session._open_guest_context(self)
            return _GuardedGuest(self, self._guest)

    def __exit__(self, _kind: object, primary: object, _traceback: object) -> None:
        try:
            self._close()
        except BaseException as close_error:
            if isinstance(primary, BaseException):
                primary.add_note(f"cleanup failed: {close_error!r}")
                return
            raise

    def _close(self) -> None:
        with self._session._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            cursors = list(self._cursors)
            errors: list[Exception] = []
            for cursor in cursors:
                try:
                    cursor.close()
                except Exception as exc:
                    errors.append(exc)
            guest, self._guest = self._guest, None
            if guest is not None:
                errors.extend(_attempt_guest_close(guest))
            self._cursors.clear()
            self._session._discard_guest(self)
            if errors:
                raise ExceptionGroup("failed to close libguestfs handle", errors)

    def _poison(self) -> list[Exception]:
        try:
            self._close()
        except ExceptionGroup as errors:
            return list(errors.exceptions)
        return []

    def _register_cursor(self, cursor: _Find0TreeCursor) -> None:
        with self._session._lifecycle_lock:
            if self._closed:
                raise RuntimeError("guest wrapper is closed")
            self._cursors.add(cursor)

    def _discard_cursor(self, cursor: _Find0TreeCursor) -> None:
        with self._session._lifecycle_lock:
            self._cursors.discard(cursor)


class _GuardedGuest:
    def __init__(self, owner: _GuestContext, guest: _Guest) -> None:
        self._owner = owner
        self._guest = guest

    def exists(self, path: str) -> int:
        return self._handle().exists(path)

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return self._handle().is_dir(path, followsymlinks=followsymlinks)

    def lstatns(self, path: str) -> dict[str, int]:
        return self._handle().lstatns(path)

    def readlink(self, path: str) -> str:
        return self._handle().readlink(path)

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return self._handle().lgetxattrs(path)

    def open_tree(self, path: str, *, limit: int) -> TreeCursor:
        self._handle()
        return _Find0TreeCursor(self._owner, self._guest, path, limit)

    def open_regular(self, path: str, *, size: int) -> AbstractContextManager[BinaryIO]:
        return self._owner._session._open_guest_regular(self._owner, self._guest, path, size)

    def create_regular(self, content: BinaryIO, path: str, *, size: int) -> None:
        self._owner._session._create_guest_regular(
            self._owner,
            self._guest,
            content,
            path,
            size,
        )

    def download_artifact(self, guest_source: str, artifact_name: str) -> None:
        self._owner._session._download_artifact(
            self._owner, self._guest, guest_source, artifact_name
        )

    def mkdir(self, path: str) -> None:
        self._handle().mkdir(path)

    def upload_artifact(self, artifact_name: str, guest_destination: str) -> None:
        self._owner._session._upload_artifact(
            self._owner, self._guest, artifact_name, guest_destination
        )

    def ln_s(self, target: str, linkname: str) -> None:
        self._handle().ln_s(target, linkname)

    def chmod(self, mode: int, path: str) -> None:
        self._handle().chmod(mode, path)

    def chown(self, owner: int, group: int, path: str) -> None:
        self._handle().chown(owner, group, path)

    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None:
        self._handle().lsetxattr(xattr, val, vallen, path)

    def mv(self, source: str, destination: str) -> None:
        self._handle().mv(source, destination)

    def rm_rf(self, path: str) -> None:
        self._handle().rm_rf(path)

    def sync(self) -> None:
        self._handle().sync()

    def _handle(self) -> _Guest:
        if self._owner._closed:
            raise RuntimeError("guest wrapper is closed")
        self._owner._session._guard_guest_operation(self._owner)
        return self._guest


_TREE_READ_CHUNK = 64 * 1024
_MAX_TREE_PATH_BYTES = 4096


class _Find0TreeCursor(TreeCursor):
    def __init__(self, owner: _GuestContext, guest: _Guest, path: str, limit: int) -> None:
        if limit < 0:
            raise ValueError("guest-tree entry limit must be nonnegative")
        self._owner = owner
        self._guest = guest
        self._path = _guest_tree_root(path)
        self._limit = limit
        self._entries: Iterator[InactiveGuestDirectoryEntry] | None = None
        self._thread: threading.Thread | None = None
        self._producer_errors: list[Exception] = []
        self._directory: str | None = None
        self._fifo: str | None = None
        self._read_fd = -1
        self._anchor_fd = -1
        self._revocation_fd = -1
        self._revocation_anchor_fd = -1
        self._done_read_fd = -1
        self._done_write_fd = -1
        self._selector: selectors.BaseSelector | None = None
        self._late_selector: selectors.BaseSelector | None = None
        self._producer_lifecycle = threading.Lock()
        self._producer_phase: Literal["pending", "dispatched", "finished"] = "pending"
        self._abandon_requested = False
        self._entered = False
        self._resources_closed = False
        self._closed = False

    def __enter__(self) -> Iterator[InactiveGuestDirectoryEntry]:
        with self._owner._session._lifecycle_lock:
            if self._closed or self._entered:
                raise RuntimeError("guest-tree cursor is closed or already entered")
            self._owner._session._guard_guest_operation(self._owner)
            self._owner._register_cursor(self)
            self._entered = True
            try:
                self._start()
            except BaseException as primary:
                self._owner._discard_cursor(self)
                self._closed = True
                for cleanup in self._cleanup_unstarted():
                    primary.add_note(f"guest-tree cursor cleanup failed: {cleanup!r}")
                raise
            return self

    def __exit__(self, _kind: object, primary: object, _traceback: object) -> None:
        try:
            self.close()
        except BaseException as close_error:
            if isinstance(primary, BaseException):
                primary.add_note(f"guest-tree cursor cleanup failed: {close_error!r}")
                return
            raise

    def __iter__(self) -> _Find0TreeCursor:
        return self

    def __next__(self) -> InactiveGuestDirectoryEntry:
        if self._closed:
            raise StopIteration
        if not self._entered:
            raise RuntimeError("guest-tree cursor must be entered before iteration")
        if self._entries is None:
            self._entries = iter(self._load_entries())
        return next(self._entries)

    def close(self) -> None:
        with self._owner._session._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if not self._resources_closed:
                    self._teardown(deliberate=True)
            finally:
                self._owner._discard_cursor(self)

    def _start(self) -> None:
        directory = tempfile.mkdtemp(prefix="kdive-find0-")
        self._directory = directory
        os.chmod(directory, 0o700)
        fifo = os.path.join(directory, "entries")
        self._fifo = fifo
        os.mkfifo(fifo, 0o600)
        self._read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self._anchor_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        self._revocation_fd, self._revocation_anchor_fd = os.pipe()
        self._done_read_fd, self._done_write_fd = os.pipe()
        selector = selectors.DefaultSelector()
        self._selector = selector
        selector.register(self._read_fd, selectors.EVENT_READ, "fifo")
        selector.register(self._done_read_fd, selectors.EVENT_READ, "done")
        late_selector = selectors.DefaultSelector()
        self._late_selector = late_selector
        late_selector.register(self._revocation_fd, selectors.EVENT_READ, "output")
        late_selector.register(self._done_read_fd, selectors.EVENT_READ, "done")
        thread = threading.Thread(
            target=self._produce,
            name="kdive-libguestfs-find0",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _produce(self) -> None:
        try:
            with self._producer_lifecycle:
                if self._abandon_requested:
                    return
                assert self._read_fd >= 0
                destination = f"/proc/self/fd/{self._read_fd}"
                self._producer_phase = "dispatched"
            self._guest.find0(self._path, destination)
        except RuntimeError as exc:
            last_errno = self._guest.last_errno()
            if last_errno == errno.EINTR:
                self._producer_errors.append(InterruptedError(errno.EINTR, str(exc)))
            else:
                self._producer_errors.append(exc)
        except Exception as exc:
            self._producer_errors.append(exc)
        finally:
            with self._producer_lifecycle:
                self._producer_phase = "finished"
            if self._done_write_fd >= 0:
                with suppress(OSError):
                    os.write(self._done_write_fd, b"\0")
                with suppress(OSError):
                    os.close(self._done_write_fd)
                self._done_write_fd = -1

    def _load_entries(self) -> list[InactiveGuestDirectoryEntry]:
        paths: list[str] = []
        pending = bytearray()
        try:
            self._read_until_done(paths, pending)
            if pending:
                raise ValueError("libguestfs find0 ended with a truncated entry")
            if len(set(paths)) != len(paths):
                raise ValueError("guest tree contains duplicate paths")
            entries = [
                InactiveGuestDirectoryEntry(path)
                for path in sorted(paths, key=lambda value: value.encode())
            ]
            with self._owner._session._lifecycle_lock:
                self._teardown(deliberate=False)
            return entries
        except BaseException as primary:
            with self._owner._session._lifecycle_lock:
                self._closed = True
                try:
                    if not self._resources_closed:
                        try:
                            self._teardown(deliberate=True)
                        except BaseException as cleanup:
                            primary.add_note(f"guest-tree cursor cleanup failed: {cleanup!r}")
                finally:
                    self._owner._discard_cursor(self)
            raise

    def _read_until_done(self, entries: list[str], pending: bytearray) -> None:
        assert self._selector is not None
        producer_done = False
        while not producer_done:
            for key, _events in self._selector.select():
                if key.data == "fifo":
                    self._read_available(entries, pending)
                else:
                    os.read(self._done_read_fd, 1)
                    producer_done = True
                    if self._anchor_fd >= 0:
                        os.close(self._anchor_fd)
                        self._anchor_fd = -1
        while self._read_available(entries, pending):
            pass

    def _read_available(self, entries: list[str], pending: bytearray) -> bool:
        try:
            chunk = os.read(self._read_fd, _TREE_READ_CHUNK)
        except BlockingIOError:
            return False
        if not chunk:
            return False
        pending.extend(chunk)
        while (delimiter := pending.find(0)) >= 0:
            value = bytes(pending[:delimiter])
            del pending[: delimiter + 1]
            if len(entries) >= self._limit:
                raise ValueError("guest tree exceeds the entry-count bound")
            entries.append(_guest_tree_relative_bytes(value))
        if len(pending) > _MAX_TREE_PATH_BYTES:
            raise ValueError("guest-tree entry exceeds the path-byte bound")
        return True

    def _teardown(self, *, deliberate: bool) -> None:
        if self._resources_closed:
            return
        self._resources_closed = True
        cleanup_errors: list[Exception] = []
        redirected = False
        with self._producer_lifecycle:
            self._abandon_requested = True
            cleanup_errors.extend(self._remove_fifo())
            if self._producer_phase == "dispatched":
                try:
                    # Keep FileOut's process-global fd number reserved while redirecting
                    # late opens to a kernel-bounded pipe until cancellation completes.
                    os.dup2(self._revocation_fd, self._read_fd, inheritable=False)
                    redirected = True
                except OSError as exc:
                    cleanup_errors.append(exc)
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            self._selector = None
        for attribute in ("_anchor_fd",):
            descriptor = getattr(self, attribute)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_errors.append(exc)
                setattr(self, attribute, -1)
        try:
            self._guest.user_cancel()
        except Exception as exc:
            cleanup_errors.append(exc)
        if redirected:
            cleanup_errors.extend(self._drain_late_transfer())
        if self._thread is not None:
            self._thread.join()
        if self._late_selector is not None:
            try:
                self._late_selector.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            self._late_selector = None
        for attribute in (
            "_read_fd",
            "_revocation_fd",
            "_revocation_anchor_fd",
            "_done_read_fd",
            "_done_write_fd",
        ):
            descriptor = getattr(self, attribute)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_errors.append(exc)
                setattr(self, attribute, -1)
        producer_errors = [
            error
            for error in self._producer_errors
            if not (deliberate and isinstance(error, OSError) and error.errno == errno.EINTR)
        ]
        cleanup_errors.extend(self._remove_output())
        if producer_errors:
            primary = producer_errors[0]
            for error in [*producer_errors[1:], *cleanup_errors]:
                primary.add_note(f"guest-tree cursor cleanup failed: {error!r}")
            raise primary
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise ExceptionGroup("failed to close guest-tree cursor", cleanup_errors)

    def _drain_late_transfer(self) -> list[Exception]:
        errors: list[Exception] = []
        selector = self._late_selector
        if selector is None:
            return [RuntimeError("late-transfer selector is not available")]
        try:
            producer_done = False
            cancel_delivered_after_output = False
            while not producer_done:
                for key, _events in selector.select():
                    if key.data == "done":
                        with suppress(OSError):
                            os.read(self._done_read_fd, 1)
                        producer_done = True
                        continue
                    try:
                        chunk = os.read(self._revocation_fd, _TREE_READ_CHUNK)
                    except OSError as exc:
                        errors.append(exc)
                        producer_done = True
                        continue
                    if chunk and not cancel_delivered_after_output:
                        try:
                            self._guest.user_cancel()
                        except Exception as exc:
                            errors.append(exc)
                        cancel_delivered_after_output = True
        except Exception as exc:
            errors.append(exc)
        return errors

    def _remove_output(self) -> list[Exception]:
        errors = self._remove_fifo()
        if self._directory is not None:
            try:
                os.rmdir(self._directory)
            except FileNotFoundError:
                pass
            except Exception as exc:
                errors.append(exc)
            self._directory = None
        return errors

    def _remove_fifo(self) -> list[Exception]:
        errors: list[Exception] = []
        if self._fifo is not None:
            try:
                os.unlink(self._fifo)
            except FileNotFoundError:
                pass
            except Exception as exc:
                errors.append(exc)
            self._fifo = None
        return errors

    def _cleanup_unstarted(self) -> list[Exception]:
        errors: list[Exception] = []
        for attribute in (
            "_read_fd",
            "_anchor_fd",
            "_revocation_fd",
            "_revocation_anchor_fd",
            "_done_read_fd",
            "_done_write_fd",
        ):
            descriptor = getattr(self, attribute)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    errors.append(exc)
                setattr(self, attribute, -1)
        for attribute in ("_selector", "_late_selector"):
            selector = getattr(self, attribute)
            if selector is not None:
                try:
                    selector.close()
                except Exception as exc:
                    errors.append(exc)
                setattr(self, attribute, None)
        errors.extend(self._remove_output())
        return errors


def _guest_tree_root(path: str) -> str:
    if (
        not path.startswith("/")
        or path == "/"
        or unicodedata.normalize("NFC", path) != path
        or any(part in {"", ".", ".."} for part in path.removeprefix("/").split("/"))
    ):
        raise ValueError("guest-tree root is not a canonical absolute path")
    return path


def _guest_tree_relative_bytes(value: bytes) -> str:
    if not value or len(value) > _MAX_TREE_PATH_BYTES:
        raise ValueError("guest-tree entry exceeds the path-byte bound")
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("guest-tree entry is not UTF-8") from exc
    if (
        path.startswith("/")
        or unicodedata.normalize("NFC", path) != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("guest-tree entry path is not a canonical relative path")
    return path


class _ConcreteSession:
    def __init__(
        self,
        *,
        system_id: UUID,
        binding: ExternalBootActivationBinding,
        pin: LocalExternalBootOperationPin,
        connection: _Connection,
        domain: _Domain,
        artifact_fd: int,
        overlay: _BoundOverlay,
        open_guest: OpenGuest,
        fstat_overlay: Callable[[int], tuple[int, int, int]],
        close_descriptor: Callable[[int], None],
        close_overlay_descriptor: Callable[[int], None],
        close_transfer_descriptor: Callable[[int], None],
        open_relative: Callable[[int, str, int, int], int],
        unlink_relative: Callable[[int, str], None],
        replace_relative: Callable[[int, str, str], None],
        fsync_descriptor: Callable[[int], None],
        temporary_artifact_name: Callable[[str], str],
        worker_pid: int,
        readiness: ReadinessProbe,
        observe_running: RunningObserver,
        cleanup_payloads: CleanupPayloads,
    ) -> None:
        self._system_id = system_id
        self._binding = binding
        self._pin: LocalExternalBootOperationPin | None = pin
        self._connection: _Connection | None = connection
        self._domain: _Domain | None = domain
        self._artifact_fd: int | None = artifact_fd
        self._overlay = overlay
        self._open_guest = open_guest
        self._fstat_overlay = fstat_overlay
        self._close_descriptor = close_descriptor
        self._close_overlay_descriptor = close_overlay_descriptor
        self._close_transfer_descriptor = close_transfer_descriptor
        self._open_relative = open_relative
        self._unlink_relative = unlink_relative
        self._replace_relative = replace_relative
        self._fsync_descriptor = fsync_descriptor
        self._temporary_artifact_name = temporary_artifact_name
        self._worker_pid = worker_pid
        self._readiness = readiness
        self._observe_running = observe_running
        self._cleanup_payloads = cleanup_payloads
        # Nested session/guest/cursor closes keep ownership until producer joins complete.
        self._lifecycle_lock = threading.RLock()
        self._guests: set[_GuestContext] = set()
        self._closed = False

    def inspect_closed(self) -> ClosedDomainInspection:
        domain = self._require_open_domain()
        xml = domain.XMLDesc(0)
        root = _parse_owned_xml(xml, self._system_id, self._overlay.path)
        active = _active(domain)
        return ClosedDomainInspection(
            xml=xml.encode(),
            active=active,
            definition_identity=_preserved_identity(root),
            source_boot_identity=_boot_identity(root),
            domain_name=domain_name_for(self._system_id),
            overlay=OverlayIdentity(self._overlay.device, self._overlay.inode),
        )

    def require_inactive(self) -> None:
        if _active(self._require_open_domain()):
            raise RuntimeError("domain must be inactive before overlay mutation")

    def stop_and_require_inactive(self) -> None:
        domain = self._require_open_domain()
        if _active(domain):
            domain.destroy()
        self.require_inactive()

    def open_artifact(self, name: str, flags: int, mode: int = 0o600) -> int:
        self._require_open_domain()
        assert self._artifact_fd is not None
        return self._open_relative(self._artifact_fd, _relative_name(name), flags, mode)

    def unlink_artifact(self, name: str) -> None:
        self._require_open_domain()
        assert self._artifact_fd is not None
        self._unlink_relative(self._artifact_fd, _relative_name(name))

    def guest(self) -> _GuestContext:
        self._require_open_domain()
        return _GuestContext(self)

    def define_xml(self, xml: str) -> None:
        self._require_no_guest_context()
        self.require_inactive()
        _parse_owned_xml(xml, self._system_id, self._overlay.path)
        assert self._connection is not None
        prior = self._domain
        replacement = self._connection.defineXML(xml)
        self._domain = replacement
        if prior is not None and prior is not replacement:
            prior.free()

    def start(self) -> None:
        self._require_no_guest_context()
        self._require_open_domain().create()

    def readiness(self) -> ReadinessResult:
        self._require_open_domain()
        return self._readiness(self._system_id)

    def observe_running(self) -> RunningKernelObservation:
        domain = self._require_open_domain()
        return self._observe_running(self._system_id, domain)

    def restore_power(self, prior: Literal["running", "inactive"]) -> None:
        domain = self._require_open_domain()
        if prior == "running":
            self._require_no_guest_context()
        active = _active(domain)
        if prior == "running" and not active:
            domain.create()
        elif prior == "inactive" and active:
            domain.destroy()

    def cleanup_payloads(self) -> None:
        self.require_inactive()
        assert self._artifact_fd is not None
        self._cleanup_payloads(self._artifact_fd, self._binding)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            guests = list(self._guests)
            errors = [error for guest in guests for error in guest._poison()]
            self._guests.clear()
            artifact_fd, self._artifact_fd = self._artifact_fd, None
            overlay_fd = self._overlay.descriptor
            domain, self._domain = self._domain, None
            connection, self._connection = self._connection, None
            pin, self._pin = self._pin, None
            for closer in (
                (lambda: self._close_descriptor(artifact_fd)) if artifact_fd is not None else None,
                lambda: self._close_overlay_descriptor(overlay_fd),
                domain.free if domain is not None else None,
                connection.close if connection is not None else None,
                pin.close if pin is not None else None,
            ):
                if closer is not None:
                    try:
                        closer()
                    except Exception as exc:  # cleanup must attempt every owned resource
                        errors.append(exc)
            if errors:
                raise ExceptionGroup("failed to close local external-boot session", errors)

    def _open_guest_context(self, wrapper: _GuestContext) -> _Guest:
        with self._lifecycle_lock:
            if self._guests:
                raise RuntimeError("an inactive guest context is already open")
            self.require_inactive()
            self._require_overlay_identity()
            guest = self._open_guest()
            try:
                guest.add_drive_opts(
                    f"/proc/{self._worker_pid}/fd/{self._overlay.descriptor}", format="qcow2"
                )
                guest.launch()
                roots = guest.inspect_os()
                if len(roots) != 1:
                    raise RuntimeError(
                        "guest inspection must find exactly one operating-system root"
                    )
                guest.mount(roots[0], "/")
            except BaseException as exc:
                for close_error in _attempt_guest_close(guest):
                    exc.add_note(f"cleanup failed: {close_error!r}")
                raise
            self._guests.add(wrapper)
            return guest

    def _discard_guest(self, guest: _GuestContext) -> None:
        with self._lifecycle_lock:
            self._guests.discard(guest)

    def _upload_artifact(
        self,
        wrapper: _GuestContext,
        guest: _Guest,
        artifact_name: str,
        guest_destination: str,
    ) -> None:
        self._guard_guest_operation(wrapper)
        descriptor = self.open_artifact(_relative_name(artifact_name), os.O_RDONLY)
        self._transfer_with_close(
            descriptor,
            lambda path: guest.upload(path, guest_destination),
        )

    def _download_artifact(
        self,
        wrapper: _GuestContext,
        guest: _Guest,
        guest_source: str,
        artifact_name: str,
    ) -> None:
        self._guard_guest_operation(wrapper)
        final_name = _relative_name(artifact_name)
        temporary_name = _relative_name(self._temporary_artifact_name(final_name))
        if temporary_name == final_name:
            raise ValueError("temporary artifact name must differ from final artifact name")
        descriptor = self.open_artifact(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            guest.download(guest_source, f"/proc/self/fd/{descriptor}")
            self._fsync_descriptor(descriptor)
            closing, descriptor = descriptor, None
            self._close_transfer_descriptor(closing)
            assert self._artifact_fd is not None
            self._replace_relative(self._artifact_fd, temporary_name, final_name)
        except BaseException as exc:
            if descriptor is not None:
                try:
                    self._close_transfer_descriptor(descriptor)
                except Exception as close_error:
                    exc.add_note(f"cleanup failed: {close_error!r}")
            assert self._artifact_fd is not None
            try:
                self._unlink_relative(self._artifact_fd, temporary_name)
            except FileNotFoundError:
                pass
            except Exception as unlink_error:
                exc.add_note(f"cleanup failed: {unlink_error!r}")
            raise

    @contextmanager
    def _open_guest_regular(
        self,
        wrapper: _GuestContext,
        guest: _Guest,
        guest_source: str,
        expected_size: int,
    ) -> Iterator[BinaryIO]:
        self._guard_guest_operation(wrapper)
        if expected_size < 0:
            raise ValueError("guest regular size must be nonnegative")
        with tempfile.TemporaryFile("w+b") as local:
            guest.download(guest_source, f"/proc/self/fd/{local.fileno()}")
            if os.fstat(local.fileno()).st_size != expected_size:
                raise ValueError("guest regular content changed during download")
            local.seek(0)
            yield local

    def _create_guest_regular(
        self,
        wrapper: _GuestContext,
        guest: _Guest,
        content: BinaryIO,
        guest_destination: str,
        expected_size: int,
    ) -> None:
        self._guard_guest_operation(wrapper)
        if expected_size < 0:
            raise ValueError("guest regular size must be nonnegative")
        with tempfile.TemporaryFile("w+b") as local:
            remaining = expected_size
            while remaining:
                chunk = content.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("guest regular content ended before declared size")
                local.write(chunk)
                remaining -= len(chunk)
            local.flush()
            local.seek(0)
            guest.upload(f"/proc/self/fd/{local.fileno()}", guest_destination)

    def _transfer_with_close(self, descriptor: int, transfer: Callable[[str], None]) -> None:
        try:
            transfer(f"/proc/self/fd/{descriptor}")
        except BaseException as exc:
            try:
                self._close_transfer_descriptor(descriptor)
            except Exception as close_error:
                exc.add_note(f"cleanup failed: {close_error!r}")
            raise
        self._close_transfer_descriptor(descriptor)

    def _guard_guest_operation(self, guest: _GuestContext) -> None:
        if guest not in self._guests:
            raise RuntimeError("guest wrapper is closed")
        self.require_inactive()
        self._require_overlay_identity()

    def _require_overlay_identity(self) -> None:
        device, inode, mode = self._fstat_overlay(self._overlay.descriptor)
        if (device, inode) != (self._overlay.device, self._overlay.inode):
            raise ValueError("System overlay changed before guest operation")
        if not stat.S_ISREG(mode):
            raise ValueError("System overlay descriptor is not a regular file")

    def _require_no_guest_context(self) -> None:
        self._require_open_domain()
        if self._guests:
            raise RuntimeError("power activation is forbidden while a guest context is open")

    def _require_open_domain(self) -> _Domain:
        if self._closed or self._domain is None:
            raise RuntimeError("local external-boot session is closed")
        return self._domain


class LocalExternalBootSessionFactory:
    def __init__(
        self,
        *,
        pin_lease: PinOperationLease,
        connect: Connect,
        open_artifact_root: OpenArtifactRoot,
        open_guest: OpenGuest,
        open_overlay: Callable[[str], int] | None = None,
        fstat_overlay: Callable[[int], tuple[int, int, int]] | None = None,
        close_descriptor: Callable[[int], None] = os.close,
        close_overlay_descriptor: Callable[[int], None] = os.close,
        close_transfer_descriptor: Callable[[int], None] = os.close,
        open_relative: Callable[[int, str, int, int], int] | None = None,
        unlink_relative: Callable[[int, str], None] | None = None,
        replace_relative: Callable[[int, str, str], None] | None = None,
        fsync_descriptor: Callable[[int], None] = os.fsync,
        temporary_artifact_name: Callable[[str], str] | None = None,
        worker_pid: int | None = None,
        readiness: ReadinessProbe | None = None,
        observe_running: RunningObserver | None = None,
        cleanup_payloads: CleanupPayloads | None = None,
    ) -> None:
        self._pin_lease = pin_lease
        self._connect = connect
        self._open_artifact_root = open_artifact_root
        self._open_guest = open_guest
        self._open_overlay = open_overlay or _open_overlay
        self._fstat_overlay = fstat_overlay or _fstat_identity
        self._close_descriptor = close_descriptor
        self._close_overlay_descriptor = close_overlay_descriptor
        self._close_transfer_descriptor = close_transfer_descriptor
        self._open_relative = open_relative or _open_relative
        self._unlink_relative = unlink_relative or _unlink_relative
        self._replace_relative = replace_relative or _replace_relative
        self._fsync_descriptor = fsync_descriptor
        self._temporary_artifact_name = temporary_artifact_name or _temporary_artifact_name
        self._worker_pid = worker_pid if worker_pid is not None else os.getpid()
        self._readiness = readiness or _unconfigured_readiness
        self._observe_running = observe_running or _unconfigured_observation
        self._cleanup_payloads = cleanup_payloads or _unconfigured_cleanup

    def open(
        self,
        lease: LocalExternalBootOperationLease,
        expected: ExpectedOperationOwnership,
    ) -> LocalExternalBootSession:
        ownership = self._pin_lease(lease)
        pin = ownership._pin
        facts = ownership.ownership
        system_id = facts.system_id
        binding = facts.binding
        binding_matches_pin = binding.system_id == str(system_id)
        binding_matches_expected = (
            system_id == expected.system_id
            and UUID(binding.run_id) == expected.run_id
            and (
                expected.activation_id is None
                or UUID(binding.activation_id) == expected.activation_id
            )
        )
        if not binding_matches_pin or not binding_matches_expected:
            mismatch = ValueError("operation lease does not match expected ownership")
            try:
                pin.close()
            except BaseException as close_error:
                mismatch.add_note(f"cleanup failed: {close_error!r}")
            raise mismatch
        connection: _Connection | None = None
        domain: _Domain | None = None
        overlay_fd: int | None = None
        artifact_fd: int | None = None
        try:
            connection = self._connect()
            expected_name = domain_name_for(system_id)
            domain = connection.lookupByName(expected_name)
            expected_overlay = overlay_path(system_id)
            try:
                inactive_xml = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "local-libvirt external-boot inactive definition could not be read",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"system_id": str(system_id)},
                ) from exc
            inactive_root = _parse_owned_xml(inactive_xml, system_id, expected_overlay)
            _require_guest_agent_channel(inactive_root, system_id)
            xml = domain.XMLDesc(0)
            _parse_owned_xml(xml, system_id, expected_overlay)
            overlay_fd = self._open_overlay(expected_overlay)
            device, inode, mode = self._fstat_overlay(overlay_fd)
            if not stat.S_ISREG(mode):
                raise ValueError("System overlay descriptor is not a regular file")
            overlay = _BoundOverlay(device, inode, expected_overlay, overlay_fd)
            artifact_fd = self._open_artifact_root(facts)
            return _ConcreteSession(
                system_id=system_id,
                binding=binding,
                pin=pin,
                connection=connection,
                domain=domain,
                artifact_fd=artifact_fd,
                overlay=overlay,
                open_guest=self._open_guest,
                fstat_overlay=self._fstat_overlay,
                close_descriptor=self._close_descriptor,
                close_overlay_descriptor=self._close_overlay_descriptor,
                close_transfer_descriptor=self._close_transfer_descriptor,
                open_relative=self._open_relative,
                unlink_relative=self._unlink_relative,
                replace_relative=self._replace_relative,
                fsync_descriptor=self._fsync_descriptor,
                temporary_artifact_name=self._temporary_artifact_name,
                worker_pid=self._worker_pid,
                readiness=self._readiness,
                observe_running=self._observe_running,
                cleanup_payloads=self._cleanup_payloads,
            )
        except BaseException as exc:
            errors: list[Exception] = []
            for closer in (
                (lambda: self._close_descriptor(artifact_fd)) if artifact_fd is not None else None,
                (
                    lambda: (
                        self._close_overlay_descriptor(overlay_fd)
                        if overlay_fd is not None
                        else None
                    )
                ),
                domain.free if domain is not None else None,
                connection.close if connection is not None else None,
                pin.close,
            ):
                if closer is not None:
                    try:
                        closer()
                    except Exception as close_error:
                        errors.append(close_error)
            for error in errors:
                exc.add_note(f"cleanup failed: {error!r}")
            raise


def _active(domain: _Domain) -> bool:
    value = domain.isActive()
    if value not in (0, 1):
        raise RuntimeError("libvirt returned an indeterminate domain state")
    return bool(value)


def _open_overlay(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_NOFOLLOW)


def _fstat_identity(descriptor: int) -> tuple[int, int, int]:
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino, value.st_mode


def _relative_name(name: str) -> str:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("artifact name must be one canonical relative segment")
    return name


def _open_relative(root_fd: int, name: str, flags: int, mode: int) -> int:
    return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=root_fd)


def _unlink_relative(root_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=root_fd)


def _replace_relative(root_fd: int, source: str, target: str) -> None:
    os.replace(source, target, src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _temporary_artifact_name(name: str) -> str:
    return f".{name}.kdive-{uuid4().hex}.tmp"


def _unconfigured_readiness(_system_id: UUID) -> ReadinessResult:
    raise RuntimeError("local external-boot readiness is not configured")


def _unconfigured_observation(_system_id: UUID, _domain: RunningDomain) -> RunningKernelObservation:
    raise RuntimeError("local external-boot running observation is not configured")


def _unconfigured_cleanup(_root_fd: int, _binding: ExternalBootActivationBinding) -> None:
    raise RuntimeError("local external-boot payload cleanup is not configured")


def _require_guest_agent_channel(root: ET.Element, system_id: UUID) -> None:
    targets = root.findall("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    if len(targets) != 1 or targets[0].get("type") != "virtio":
        raise CategorizedError(
            "local-libvirt external-boot requires reprovisioning: "
            "the qemu-guest-agent channel is absent or malformed",
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
            terminal=True,
        )


def _parse_owned_xml(xml: str, system_id: UUID, expected_overlay: str) -> ET.Element:
    if unicodedata.normalize("NFC", xml) != xml:
        raise ValueError("domain XML must be NFC")
    try:
        root = _safe_fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("domain XML is malformed or forbidden") from exc
    if root.tag != "domain" or root.findtext("name") != domain_name_for(system_id):
        raise ValueError("domain ownership does not match the operation lease")
    if root.findtext(f"metadata/{{{KDIVE_METADATA_NS}}}system") != str(system_id):
        raise ValueError("domain ownership metadata does not match the operation lease")
    disks = []
    for disk in root.findall("devices/disk"):
        source = disk.find("source")
        driver = disk.find("driver")
        target = disk.find("target")
        if (
            disk.get("type") == "file"
            and disk.get("device") == "disk"
            and source is not None
            and source.get("file") == expected_overlay
            and driver is not None
            and driver.get("type") == "qcow2"
            and target is not None
            and target.get("dev") == "vda"
            and target.get("bus") == "virtio"
            and disk.find("readonly") is None
        ):
            disks.append(disk)
    if len(disks) != 1:
        raise ValueError("domain overlay ownership is absent or ambiguous")
    return root


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()


def _preserved_identity(root: ET.Element) -> str:
    cloned = ET.fromstring(ET.tostring(root, encoding="unicode"))  # noqa: S314 - defused above
    os_element = cloned.find("os")
    if os_element is not None:
        for tag in ("kernel", "initrd", "cmdline"):
            element = os_element.find(tag)
            if element is not None:
                os_element.remove(element)
    for element in cloned.iter():
        if len(element) and element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    canonical = ET.canonicalize(
        ET.tostring(cloned, encoding="unicode"),
        with_comments=False,
        strip_text=False,
        rewrite_prefixes=True,
    ).encode()
    return _digest(b"kdive-libvirt-preserved-v1", canonical)


def _boot_identity(root: ET.Element) -> str:
    os_element = root.find("os")
    value = {
        "cmdline": os_element.findtext("cmdline") if os_element is not None else None,
        "initrd": os_element.findtext("initrd") if os_element is not None else None,
        "kernel": os_element.findtext("kernel") if os_element is not None else None,
        "schema": "libvirt-boot-projection-v1",
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(b"kdive-libvirt-boot-projection-v1", payload)


def _attempt_guest_close(guest: _Guest) -> list[Exception]:
    errors: list[Exception] = []
    for closer in (guest.shutdown, guest.close):
        try:
            closer()
        except Exception as exc:
            errors.append(exc)
    return errors
