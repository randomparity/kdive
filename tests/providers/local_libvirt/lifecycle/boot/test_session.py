from __future__ import annotations

import errno
import io
import os
import queue
import selectors
import stat
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LibguestfsAuthenticatedGuestTree,
)
from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    ModuleArchiveCapture,
    RealGuestRecoveryWriter,
    RecoveryArchiveSink,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ExpectedOperationOwnership,
    LocalExternalBootOperationLease,
    LocalExternalBootSessionFactory,
    OpenArtifactRoot,
    OperationOwnership,
    PinnedOperationOwnership,
    PinOperationLease,
    _ConcreteSession,
    _Find0TreeCursor,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    RunningKernelObservation,
)

SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")
BINDING = ExternalBootActivationBinding(
    system_id=str(SYSTEM_ID),
    run_id="22222222-2222-2222-2222-222222222222",
    activation_id="33333333-3333-3333-3333-333333333333",
)
OVERLAY = f"/var/lib/kdive/rootfs/{SYSTEM_ID}-overlay.qcow2"
ACTIVATION_ID = UUID(BINDING.activation_id)


class FakeLease:
    def __init__(self) -> None:
        self.system_id = SYSTEM_ID
        self.binding = BINDING
        self.released = False
        self.pins = 0

    def release(self) -> None:
        if self.pins:
            raise RuntimeError("operation lease is pinned")
        self.released = True


class FakePin:
    def __init__(self, lease: FakeLease) -> None:
        self.lease = lease
        lease.pins += 1

    def close(self) -> None:
        self.lease.pins -= 1


class FakeLane:
    def issue(self) -> LocalExternalBootOperationLease:
        return FakeLease()

    def pin(self, lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        if not isinstance(lease, FakeLease):
            raise TypeError("foreign operation lease")
        if lease.released:
            raise RuntimeError("operation lease is released")
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding), FakePin(lease)
        )


LANE = FakeLane()


def _lease() -> FakeLease:
    return FakeLease()


def _expected(*, activation_id: UUID | None = ACTIVATION_ID) -> ExpectedOperationOwnership:
    return ExpectedOperationOwnership(
        system_id=SYSTEM_ID,
        run_id=UUID(BINDING.run_id),
        activation_id=activation_id,
    )


@pytest.mark.parametrize(
    "expected",
    [
        ExpectedOperationOwnership(UUID(int=9), UUID(BINDING.run_id), UUID(BINDING.activation_id)),
        ExpectedOperationOwnership(SYSTEM_ID, UUID(int=9), UUID(BINDING.activation_id)),
        ExpectedOperationOwnership(SYSTEM_ID, UUID(BINDING.run_id), UUID(int=9)),
    ],
)
def test_expected_ownership_rejects_substitution_before_resource_open(
    expected: ExpectedOperationOwnership,
) -> None:
    events: list[str] = []
    lease = _lease()

    with pytest.raises(ValueError, match="expected ownership"):
        _factory(events).open(lease, expected)

    assert events == []
    lease.release()


def test_expected_ownership_without_activation_accepts_exact_system_and_run() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease(), _expected(activation_id=None))
    session.close()

    assert "connection.open" in events

    with pytest.raises(FrozenInstanceError):
        _expected().run_id = UUID(int=9)  # ty: ignore[invalid-assignment]


def test_expected_ownership_mismatch_preserves_rejection_when_pin_close_fails() -> None:
    events: list[str] = []

    class CloseFaultPin:
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            raise OSError("pin close")

    pin = CloseFaultPin()

    def pin_lease(lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        del lease
        return PinnedOperationOwnership(OperationOwnership(SYSTEM_ID, BINDING), pin)

    crossed = ExpectedOperationOwnership(UUID(int=9), UUID(BINDING.run_id), ACTIVATION_ID)
    with pytest.raises(ValueError, match="expected ownership") as raised:
        _factory(events, pin_lease=pin_lease).open(_lease(), crossed)

    assert raised.value.__notes__ == ["cleanup failed: OSError('pin close')"]
    assert pin.close_attempts == 1
    assert events == []


def _xml(*, overlay: str = OVERLAY, system_id: UUID = SYSTEM_ID) -> str:
    return (
        "<domain><name>kdive-" + str(system_id) + "</name><metadata>"
        '<kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">'
        + str(system_id)
        + "</kdive:system></metadata><os><kernel>/old</kernel><cmdline>root=x</cmdline></os>"
        '<devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/>'
        f'<source file="{overlay}"/><target dev="vda" bus="virtio"/></disk></devices></domain>'
    )


class Domain:
    def __init__(self, events: list[str], xml: str | None = None) -> None:
        self.events = events
        self.xml = xml or _xml()
        self.active = False

    def XMLDesc(self, flags: int) -> str:  # noqa: N802
        del flags
        self.events.append("domain.xml")
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        self.events.append("domain.active")
        return int(self.active)

    def destroy(self) -> int:
        self.events.append("domain.destroy")
        self.active = False
        return 0

    def create(self) -> int:
        self.events.append("domain.create")
        self.active = True
        return 0

    def free(self) -> None:
        self.events.append("domain.close")


class Conn:
    def __init__(self, events: list[str], domain: Domain) -> None:
        self.events = events
        self.domain = domain

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        self.events.append(f"domain.open:{name}")
        return self.domain

    def defineXML(self, xml: str) -> Domain:  # noqa: N802
        self.events.append("domain.define")
        self.domain.xml = xml
        return self.domain

    def close(self) -> None:
        self.events.append("connection.close")


class Guest:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def add_drive_opts(self, overlay: str, *, format: str) -> None:
        self.events.append(f"guest.drive:{overlay}:{format}")

    def launch(self) -> None:
        self.events.append("guest.launch")

    def inspect_os(self) -> list[str]:
        self.events.append("guest.inspect")
        return ["/dev/sda1"]

    def mount(self, device: str, mountpoint: str) -> None:
        self.events.append(f"guest.mount:{device}:{mountpoint}")

    def shutdown(self) -> None:
        self.events.append("guest.shutdown")

    def close(self) -> None:
        self.events.append("guest.close")

    def exists(self, path: str) -> int:
        self.events.append(f"guest.exists:{path}")
        return 1

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return int(bool(path) and not followsymlinks)

    def lstatns(self, path: str) -> dict[str, int]:
        return {"st_mode": len(path)}

    def readlink(self, path: str) -> str:
        return path

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return [{"attrname": path}]

    def download(self, remotefilename: str, filename: str) -> None:
        self.events.append(f"download:{remotefilename}:{filename}")

    def mkdir(self, path: str) -> None:
        self.events.append(f"mkdir:{path}")

    def upload(self, filename: str, remotefilename: str) -> None:
        self.events.append(f"upload:{filename}:{remotefilename}")

    def ln_s(self, target: str, linkname: str) -> None:
        self.events.append(f"ln:{target}:{linkname}")

    def chmod(self, mode: int, path: str) -> None:
        self.events.append(f"chmod:{mode}:{path}")

    def chown(self, owner: int, group: int, path: str) -> None:
        self.events.append(f"chown:{owner}:{group}:{path}")

    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None:
        self.events.append(f"xattr:{xattr}:{val!r}:{vallen}:{path}")

    def rm_rf(self, path: str) -> None:
        self.events.append(f"rm:{path}")

    def find0(self, directory: str, files: str) -> None:
        del directory, files
        raise AssertionError("test must provide an instrumented find0 producer")

    def user_cancel(self) -> None:
        self.events.append("guest.cancel")


class Find0Guest(Guest):
    def __init__(
        self,
        events: list[str],
        entries: list[bytes],
        *,
        producer_fault: BaseException | None = None,
        cancellation_fault: BaseException | None = None,
        wait_before_write: bool = False,
        cooperate_with_cancel: bool = True,
    ) -> None:
        super().__init__(events)
        self.entries = entries
        self.producer_fault = producer_fault
        self.cancellation_fault = cancellation_fault
        self.wait_before_write = wait_before_write
        self.cooperate_with_cancel = cooperate_with_cancel
        self.producer_started = threading.Event()
        self.producer_finished = threading.Event()
        self.cancel_requested = threading.Event()
        self.release_producer = threading.Event()
        self.output_path: Path | None = None
        self.output_was_fifo = False
        self.output_parent_mode: int | None = None
        self.find0_calls = 0

    def find0(self, directory: str, files: str) -> None:
        self.find0_calls += 1
        self.events.append(f"guest.find0:{directory}")
        try:
            self.output_path = Path(os.readlink(files))
        except OSError:
            self.output_path = Path(files)
        self.output_was_fifo = stat.S_ISFIFO(os.stat(files).st_mode)
        self.output_parent_mode = stat.S_IMODE(os.stat(self.output_path.parent).st_mode)
        try:
            with open(files, "wb", buffering=0) as output:
                self.producer_started.set()
                if self.wait_before_write:
                    assert self.release_producer.wait(timeout=5), (
                        "producer release was not signaled"
                    )
                    if self.cancel_requested.is_set():
                        raise self.cancellation_fault or OSError(
                            errno.EINTR, "find0 deliberately canceled"
                        )
                for entry in self.entries:
                    output.write(entry + b"\0")
            if self.producer_fault is not None:
                raise self.producer_fault
        finally:
            self.producer_finished.set()

    def user_cancel(self) -> None:
        super().user_cancel()
        self.cancel_requested.set()
        if self.cooperate_with_cancel:
            self.release_producer.set()

    def find(self, _path: str) -> list[str]:
        raise AssertionError("list-returning find must not be called")

    def readdir(self, _path: str) -> list[str]:
        raise AssertionError("readdir must not be called")

    def ls(self, _path: str) -> list[str]:
        raise AssertionError("ls must not be called")

    def glob_expand(self, _pattern: str) -> list[str]:
        raise AssertionError("globbing must not be called")


def _stream_session(
    events: list[str],
    guest: Find0Guest,
    lease: FakeLease | None = None,
    *,
    pin_lease: PinOperationLease = LANE.pin,
) -> tuple[_ConcreteSession, FakeLease]:
    selected_lease = lease or _lease()
    factory = LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: guest,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = cast(_ConcreteSession, factory.open(selected_lease, _expected()))
    return session, selected_lease


def _recording_pin_lease(
    events: list[str], producer: Find0Guest, lease: FakeLease
) -> tuple[PinOperationLease, list[tuple[bool, int, bool]]]:
    observations: list[tuple[bool, int, bool]] = []

    class RecordingPin(FakePin):
        def close(self) -> None:
            assert producer.output_path is not None
            observations.append(
                (
                    producer.producer_finished.is_set(),
                    events.count("guest.close"),
                    producer.output_path.parent.exists(),
                )
            )
            events.append("pin.close")
            super().close()

    def pin_lease(candidate: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert candidate is lease
        return PinnedOperationOwnership(
            OperationOwnership(candidate.system_id, candidate.binding), RecordingPin(lease)
        )

    return pin_lease, observations


def _start_call(
    call: Callable[[], None],
    *,
    name: str,
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            call()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, finished, errors


class LifecycleLockProbe:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts: dict[str, threading.Event] = {}

    def expect_attempt(self, thread_name: str) -> threading.Event:
        attempt = threading.Event()
        assert thread_name not in self._attempts
        self._attempts[thread_name] = attempt
        return attempt

    def __enter__(self) -> bool:
        attempt = self._attempts.get(threading.current_thread().name)
        if attempt is None:
            return self._lock.acquire()
        acquired = self._lock.acquire(blocking=False)
        attempt.set()
        return acquired or self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


def _instrument_lifecycle_lock(session: _ConcreteSession) -> LifecycleLockProbe:
    lifecycle_lock = LifecycleLockProbe()
    session.__dict__["_lifecycle_lock"] = lifecycle_lock
    return lifecycle_lock


def test_find0_tree_streams_one_multilevel_walk_and_byte_sorts_after_completion() -> None:
    events: list[str] = []
    producer = Find0Guest(events, [b"z.ko", b"kernel", b"kernel/a.ko", b"a.ko"])
    session, _lease_value = _stream_session(events, producer)

    with session.guest() as guest, guest.open_tree("/lib/modules/6.12.0", limit=4) as entries:
        assert [entry.path for entry in entries] == [
            "a.ko",
            "kernel",
            "kernel/a.ko",
            "z.ko",
        ]

    assert producer.find0_calls == 1
    assert producer.output_was_fifo
    assert producer.output_parent_mode == 0o700
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    assert producer.cancel_requested.is_set()
    assert producer.producer_finished.is_set()
    session.close()


def test_find0_tree_limit_plus_one_rejects_without_retaining_the_extra_entry() -> None:
    events: list[str] = []
    producer = Find0Guest(events, [b"a", b"b", b"not-visited"])
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(ValueError, match="entry-count"),
        guest.open_tree("/lib/modules/6.12.0", limit=2) as entries,
    ):
        list(entries)

    assert producer.find0_calls == 1
    assert producer.cancel_requested.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    session.close()


def test_find0_tree_rejects_oversized_complete_entry_before_retention() -> None:
    read_fd, write_fd = os.pipe()
    cursor = object.__new__(_Find0TreeCursor)
    cursor._read_fd = read_fd
    cursor._limit = 1
    entries: list[str] = []
    try:
        os.write(write_fd, b"a" * 4097 + b"\0")
        with pytest.raises(ValueError, match="path-byte"):
            cursor._read_available(entries, bytearray())
    finally:
        os.close(write_fd)
        os.close(read_fd)

    assert entries == []


def test_find0_tree_preserves_complete_entry_validation_when_cleanup_fails() -> None:
    events: list[str] = []

    class CleanupFaultGuest(Find0Guest):
        def user_cancel(self) -> None:
            super().user_cancel()
            raise OSError("cancel secondary")

    producer = CleanupFaultGuest(events, [b"a" * 4097])
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(ValueError, match="path-byte") as raised,
        guest.open_tree("/lib/modules/6.12.0", limit=1) as entries,
    ):
        list(entries)

    assert raised.value.__notes__ == [
        "guest-tree cursor cleanup failed: OSError('cancel secondary')"
    ]
    session.close()


def test_find0_tree_early_close_cancels_and_joins_blocked_producer() -> None:
    events: list[str] = []
    producer = Find0Guest(events, [], wait_before_write=True)
    session, _lease_value = _stream_session(events, producer)

    with session.guest() as guest:
        cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
        cursor.__enter__()
        assert producer.producer_started.wait(timeout=5)
        cursor.close()

    assert producer.cancel_requested.is_set()
    assert producer.producer_finished.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    session.close()


def test_find0_tree_normalizes_python_binding_cancellation_on_early_close() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        cancellation_fault=RuntimeError("find0: transfer was cancelled"),
        wait_before_write=True,
    )
    session, _lease_value = _stream_session(events, producer)

    with session.guest() as guest:
        cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
        cursor.__enter__()
        assert producer.producer_started.wait(timeout=5)
        cursor.close()

    assert producer.cancel_requested.is_set()
    assert producer.producer_finished.is_set()
    session.close()


def test_find0_tree_does_not_normalize_unrelated_python_binding_runtime_error() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [b"a"],
        producer_fault=RuntimeError("find0: appliance disconnected"),
    )
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(RuntimeError, match="find0: appliance disconnected"),
        guest.open_tree("/lib/modules/6.12.0", limit=2) as entries,
    ):
        list(entries)

    session.close()


def test_find0_tree_reports_file_not_found_after_deliberate_cancellation() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cancellation_fault=FileNotFoundError("backend path disappeared"),
    )
    pin_observations: list[tuple[bool, bool]] = []

    class TrackingPin(FakePin):
        def close(self) -> None:
            assert producer.output_path is not None
            pin_observations.append(
                (producer.producer_finished.is_set(), producer.output_path.parent.exists())
            )
            events.append("pin.close")
            super().close()

    def pin_lease(lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert isinstance(lease, FakeLease)
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding), TrackingPin(lease)
        )

    lease = _lease()
    session = LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: producer,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    ).open(lease, _expected())
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    cursor.__enter__()
    assert producer.producer_started.wait(timeout=5)

    with pytest.raises(FileNotFoundError, match="backend path disappeared"):
        cursor.close()

    assert producer.producer_finished.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    assert lease.pins == 1
    guest_context.__exit__(None, None, None)
    session.close()
    assert pin_observations == [(True, False)]
    assert lease.pins == 0
    assert events[-1] == "pin.close"


def test_session_close_abandons_cursor_before_find0_and_releases_pin_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    outcomes: queue.Queue[str] = queue.Queue()
    producer_at_gate = threading.Event()
    release_producer = threading.Event()

    class BeforeOpenGuest(Find0Guest):
        def find0(self, directory: str, files: str) -> None:
            outcomes.put("find0")
            super().find0(directory, files)

    class TrackingPin(FakePin):
        def close(self) -> None:
            events.append("pin.close")
            super().close()

    def pin_lease(lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert isinstance(lease, FakeLease)
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding), TrackingPin(lease)
        )

    original_produce = _Find0TreeCursor._produce

    def gated_produce(cursor: _Find0TreeCursor) -> None:
        producer_at_gate.set()
        assert release_producer.wait(timeout=5), "producer release was not signaled"
        original_produce(cursor)

    monkeypatch.setattr(_Find0TreeCursor, "_produce", gated_produce)
    producer = BeforeOpenGuest(events, [])
    lease = _lease()
    factory = LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: producer,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease, _expected())
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    assert isinstance(cursor, _Find0TreeCursor)
    cursor.__enter__()
    assert producer_at_gate.wait(timeout=5)
    assert cursor._fifo is not None
    fifo = cursor._fifo
    close_error: list[BaseException] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            outcomes.put("closed")

    closer = threading.Thread(target=close_session)
    closer.start()
    assert producer.cancel_requested.wait(timeout=5)
    assert lease.pins == 1
    release_producer.set()
    first_outcome = outcomes.get(timeout=5)
    rescue_fd = -1
    if first_outcome == "find0":
        rescue_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        assert outcomes.get(timeout=5) == "closed"
    closer.join()
    if rescue_fd >= 0:
        os.close(rescue_fd)

    assert first_outcome == "closed"
    assert producer.find0_calls == 0
    assert close_error == []
    assert lease.pins == 0
    assert not Path(fifo).parent.exists()
    assert events[-1] == "pin.close"


@pytest.mark.parametrize("fail_at", [1, 2])
def test_find0_tree_selector_allocation_fault_fails_setup_closed(
    fail_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = Find0Guest(events, [])
    session, _lease_value = _stream_session(events, producer)
    real_default_selector = selectors.DefaultSelector
    allocation_count = 0

    class TrackingSelector:
        def __init__(self) -> None:
            self.delegate = real_default_selector()
            self.closed = False

        def register(
            self, fileobj: int, events: int, data: object | None = None
        ) -> selectors.SelectorKey:
            return self.delegate.register(fileobj, events, data)

        def close(self) -> None:
            self.delegate.close()
            self.closed = True

    constructed: list[TrackingSelector] = []

    def allocate_selector() -> selectors.BaseSelector:
        nonlocal allocation_count
        allocation_count += 1
        if allocation_count == fail_at:
            raise OSError(f"selector allocation {fail_at}")
        selector = TrackingSelector()
        constructed.append(selector)
        return selector  # ty: ignore[invalid-return-type]

    monkeypatch.setattr(selectors, "DefaultSelector", allocate_selector)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    try:
        with pytest.raises(OSError, match=f"selector allocation {fail_at}"):
            cursor.__enter__()
    finally:
        monkeypatch.setattr(selectors, "DefaultSelector", real_default_selector)
        if isinstance(cursor, _Find0TreeCursor) and cursor._entered and not cursor._closed:
            cursor.close()
        guest_context.__exit__(None, None, None)
        session.close()

    assert producer.find0_calls == 0
    assert all(selector.closed for selector in constructed)
    assert isinstance(cursor, _Find0TreeCursor)
    assert cursor._directory is None
    assert cursor._fifo is None
    assert all(
        getattr(cursor, attribute) == -1
        for attribute in (
            "_read_fd",
            "_anchor_fd",
            "_revocation_fd",
            "_revocation_anchor_fd",
            "_done_read_fd",
            "_done_write_fd",
        )
    )


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
def test_find0_tree_selector_registration_fault_fails_setup_closed(
    fail_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = Find0Guest(events, [])
    session, _lease_value = _stream_session(events, producer)
    real_default_selector = selectors.DefaultSelector
    registration_count = 0

    class FaultingSelector:
        def __init__(self) -> None:
            self.delegate = real_default_selector()
            self.closed = False
            constructed.append(self)

        def register(
            self, fileobj: int, events: int, data: object | None = None
        ) -> selectors.SelectorKey:
            nonlocal registration_count
            registration_count += 1
            if registration_count == fail_at:
                raise OSError(f"selector registration {fail_at}")
            return self.delegate.register(fileobj, events, data)

        def close(self) -> None:
            self.delegate.close()
            self.closed = True

    constructed: list[FaultingSelector] = []

    monkeypatch.setattr(selectors, "DefaultSelector", FaultingSelector)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    try:
        with pytest.raises(OSError, match=f"selector registration {fail_at}"):
            cursor.__enter__()
    finally:
        monkeypatch.setattr(selectors, "DefaultSelector", real_default_selector)
        if isinstance(cursor, _Find0TreeCursor) and cursor._entered and not cursor._closed:
            cursor.close()
        guest_context.__exit__(None, None, None)
        session.close()

    assert producer.find0_calls == 0
    assert all(selector.closed for selector in constructed)
    assert isinstance(cursor, _Find0TreeCursor)
    assert cursor._directory is None
    assert cursor._fifo is None
    assert all(
        getattr(cursor, attribute) == -1
        for attribute in (
            "_read_fd",
            "_anchor_fd",
            "_revocation_fd",
            "_revocation_anchor_fd",
            "_done_read_fd",
            "_done_write_fd",
        )
    )


def test_find0_tree_selector_registration_preserves_primary_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = Find0Guest(events, [])
    session, _lease_value = _stream_session(events, producer)
    real_default_selector = selectors.DefaultSelector
    registration_count = 0

    class CloseFaultSelector:
        def __init__(self) -> None:
            self.delegate = real_default_selector()

        def register(
            self, fileobj: int, events: int, data: object | None = None
        ) -> selectors.SelectorKey:
            nonlocal registration_count
            registration_count += 1
            if registration_count == 2:
                raise OSError("selector registration")
            return self.delegate.register(fileobj, events, data)

        def close(self) -> None:
            self.delegate.close()
            raise OSError("selector close")

    monkeypatch.setattr(selectors, "DefaultSelector", CloseFaultSelector)
    with session.guest() as guest:
        cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
        with pytest.raises(OSError, match="selector registration") as raised:
            cursor.__enter__()

    assert raised.value.__notes__ == ["guest-tree cursor cleanup failed: OSError('selector close')"]
    assert producer.find0_calls == 0
    session.close()


def test_session_close_does_not_allocate_selector_before_producer_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    session, _lease_value = _stream_session(events, producer, lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    cursor.__enter__()
    assert isinstance(cursor, _Find0TreeCursor)
    assert producer.producer_started.wait(timeout=5)
    allocation_attempts = 0

    def fail_selector_allocation() -> selectors.BaseSelector:
        nonlocal allocation_attempts
        allocation_attempts += 1
        raise OSError("teardown selector allocation")

    monkeypatch.setattr(selectors, "DefaultSelector", fail_selector_allocation)
    close_finished = threading.Event()
    close_error: list[BaseException] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    try:
        assert producer.cancel_requested.wait(timeout=5)
        assert not close_finished.is_set()
        assert lease.pins == 1
    finally:
        producer.release_producer.set()
        closer.join(timeout=5)
        if cursor._thread is not None:
            cursor._thread.join(timeout=5)
        cleanup_errors = cursor._cleanup_unstarted()

    assert cleanup_errors == []
    assert close_finished.is_set()
    assert close_error == []
    assert allocation_attempts == 0
    assert producer.producer_finished.is_set()
    assert lease.pins == 0


def test_session_close_retains_pin_when_late_selector_select_fails() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    session, _lease_value = _stream_session(events, producer, lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    cursor.__enter__()
    assert isinstance(cursor, _Find0TreeCursor)
    assert producer.producer_started.wait(timeout=5)

    class SelectFaultSelector:
        def select(self) -> object:
            raise OSError("late selector select")

        def close(self) -> None:
            return

    cursor._late_selector = SelectFaultSelector()  # ty: ignore[invalid-assignment]
    close_finished = threading.Event()
    close_error: list[BaseException] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    try:
        assert producer.cancel_requested.wait(timeout=5)
        assert not close_finished.is_set()
        assert lease.pins == 1
    finally:
        producer.release_producer.set()
        closer.join(timeout=5)

    assert close_finished.is_set()
    assert producer.producer_finished.is_set()
    assert lease.pins == 0
    assert len(close_error) == 1
    assert isinstance(close_error[0], ExceptionGroup)
    assert any(str(error) == "late selector select" for error in close_error[0].exceptions)


def test_session_close_reserves_fileout_fd_until_producer_exit_under_reuse_pressure(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    producer_called = threading.Event()
    release_destination_open = threading.Event()
    close_finished = threading.Event()
    pin_observations: list[tuple[bool, bool]] = []
    unrelated = tmp_path / "unrelated"
    original_content = b"must remain unchanged"
    unrelated.write_bytes(original_content)

    class BeforeDestinationOpenGuest(Find0Guest):
        destination_mode: int | None = None
        destination_open_error: BaseException | None = None

        def __init__(self) -> None:
            super().__init__(events, [], cooperate_with_cancel=False)

        def find0(self, directory: str, files: str) -> None:
            self.find0_calls += 1
            self.events.append(f"guest.find0:{directory}")
            self.output_path = Path(files)
            producer_called.set()
            try:
                assert release_destination_open.wait(timeout=5), (
                    "destination-open release was not signaled"
                )
                with open(files, "wb", buffering=0) as output:
                    self.destination_mode = os.fstat(output.fileno()).st_mode
                    output.write(b"stale FileOut write")
            except BaseException as exc:
                self.destination_open_error = exc
                raise
            finally:
                self.producer_finished.set()

    output_directory: list[Path] = []

    class TrackingPin(FakePin):
        def close(self) -> None:
            pin_observations.append(
                (producer.producer_finished.is_set(), output_directory[0].exists())
            )
            events.append("pin.close")
            super().close()

    def pin_lease(lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert isinstance(lease, FakeLease)
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding), TrackingPin(lease)
        )

    producer = BeforeDestinationOpenGuest()
    lease = _lease()
    factory = LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: producer,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease, _expected())
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    assert isinstance(cursor, _Find0TreeCursor)
    cursor.__enter__()
    assert producer_called.wait(timeout=5)
    assert cursor._fifo is not None
    fifo = Path(cursor._fifo)
    fileout_fd = cursor._read_fd
    revocation_fd = cursor._revocation_fd
    output_directory.append(fifo.parent)
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    close_error: list[BaseException] = []
    pressure_fds: list[int] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    try:
        assert producer.cancel_requested.wait(timeout=5)
        assert not close_finished.is_set()
        assert lease.pins == 1
        assert not fifo.exists()
        while not pressure_fds or pressure_fds[-1] <= fileout_fd:
            pressure_fds.append(os.open(unrelated, os.O_RDWR))
        release_destination_open.set()
        assert close_finished.wait(timeout=5)
        closer.join()

        assert close_error == []
        assert unrelated.read_bytes() == original_content
        assert fileout_fd not in pressure_fds
        assert revocation_fd not in pressure_fds
        with pytest.raises(OSError):
            os.fstat(fileout_fd)
        with pytest.raises(OSError):
            os.fstat(revocation_fd)
        assert producer.destination_open_error is None
        assert producer.destination_mode is not None
        assert stat.S_ISFIFO(producer.destination_mode)
        assert producer.cancel_requested.is_set()
        assert not fifo.parent.exists()
        assert pin_observations == [(True, False)]
        assert lease.pins == 0
        assert events[-1] == "pin.close"
    finally:
        release_destination_open.set()
        closer.join(timeout=5)
        for descriptor in pressure_fds:
            os.close(descriptor)
        if fifo.exists():
            fifo.unlink()
        if fifo.parent.exists():
            fifo.parent.rmdir()


def test_session_close_recancels_find0_after_late_transfer_becomes_active() -> None:
    events: list[str] = []
    at_dispatch_boundary = threading.Event()
    release_transfer = threading.Event()
    transfer_active = threading.Event()
    idle_cancel = threading.Event()
    active_cancel = threading.Event()
    close_finished = threading.Event()
    output_directory: list[Path] = []
    pin_observations: list[tuple[bool, bool]] = []

    class LateTransferGuest(Find0Guest):
        destination_mode: int | None = None

        def __init__(self) -> None:
            super().__init__(events, [])

        def find0(self, directory: str, files: str) -> None:
            self.find0_calls += 1
            self.events.append(f"guest.find0:{directory}")
            at_dispatch_boundary.set()
            try:
                assert release_transfer.wait(timeout=5), "transfer release was not signaled"
                with open(files, "wb", buffering=0) as output:
                    self.destination_mode = os.fstat(output.fileno()).st_mode
                    transfer_active.set()
                    output.write(b"x" * (2 * 65_536))
                if active_cancel.is_set():
                    raise RuntimeError("find0: transfer was cancelled")
            finally:
                self.producer_finished.set()

        def user_cancel(self) -> None:
            Guest.user_cancel(self)
            if transfer_active.is_set():
                active_cancel.set()
            else:
                idle_cancel.set()

    class TrackingPin(FakePin):
        def close(self) -> None:
            pin_observations.append(
                (producer.producer_finished.is_set(), output_directory[0].exists())
            )
            events.append("pin.close")
            super().close()

    def pin_lease(lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert isinstance(lease, FakeLease)
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding), TrackingPin(lease)
        )

    producer = LateTransferGuest()
    lease = _lease()
    factory = LocalExternalBootSessionFactory(
        pin_lease=pin_lease,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: producer,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease, _expected())
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
    assert isinstance(cursor, _Find0TreeCursor)
    cursor.__enter__()
    assert at_dispatch_boundary.wait(timeout=5)
    assert cursor._fifo is not None
    output_directory.append(Path(cursor._fifo).parent)
    close_error: list[BaseException] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    rescue: threading.Thread | None = None
    try:
        assert idle_cancel.wait(timeout=5)
        assert not active_cancel.is_set()
        assert lease.pins == 1
        release_transfer.set()
        assert transfer_active.wait(timeout=5)
        assert close_finished.wait(timeout=5)
        closer.join()
    finally:
        release_transfer.set()
        if not close_finished.is_set():

            def drain_revocation_pipe() -> None:
                while not producer.producer_finished.is_set():
                    try:
                        if not os.read(cursor._revocation_fd, 65_536):
                            return
                    except OSError:
                        return

            rescue = threading.Thread(target=drain_revocation_pipe)
            rescue.start()
        closer.join(timeout=5)
        if rescue is not None:
            rescue.join(timeout=5)

    assert close_error == []
    assert active_cancel.is_set()
    assert producer.find0_calls == 1
    assert producer.destination_mode is not None
    assert stat.S_ISFIFO(producer.destination_mode)
    assert not output_directory[0].exists()
    assert pin_observations == [(True, False)]
    assert lease.pins == 0
    assert events[-1] == "pin.close"


@pytest.mark.parametrize("fault_at", ["chmod", "mkfifo"])
def test_find0_tree_setup_fault_removes_owned_directory(
    fault_at: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    producer = Find0Guest(events, [])
    session, _lease_value = _stream_session(events, producer)
    directory = tmp_path / "owned-find0"

    def make_directory(*, prefix: str) -> str:
        assert prefix == "kdive-find0-"
        directory.mkdir()
        return str(directory)

    def fail_chmod(_path: str, _mode: int) -> None:
        raise PermissionError("chmod setup")

    def fail_mkfifo(_path: str, _mode: int) -> None:
        raise OSError("mkfifo setup")

    monkeypatch.setattr("tempfile.mkdtemp", make_directory)
    if fault_at == "chmod":
        monkeypatch.setattr(os, "chmod", fail_chmod)
    else:
        monkeypatch.setattr(os, "mkfifo", fail_mkfifo)

    with session.guest() as guest:
        cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
        with pytest.raises(OSError, match=f"{fault_at} setup"):
            cursor.__enter__()

    assert not directory.exists()
    session.close()


def test_find0_tree_setup_fault_preserves_primary_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    producer = Find0Guest(events, [])
    session, _lease_value = _stream_session(events, producer)
    directory = tmp_path / "owned-find0"
    original_rmdir = os.rmdir

    def make_directory(*, prefix: str) -> str:
        assert prefix == "kdive-find0-"
        directory.mkdir()
        return str(directory)

    def fail_mkfifo(_path: str, _mode: int) -> None:
        raise OSError("mkfifo setup")

    def fail_rmdir(path: str) -> None:
        if Path(path) == directory:
            raise OSError("rmdir cleanup")
        original_rmdir(path)

    monkeypatch.setattr("tempfile.mkdtemp", make_directory)
    monkeypatch.setattr(os, "mkfifo", fail_mkfifo)
    monkeypatch.setattr(os, "rmdir", fail_rmdir)
    try:
        with session.guest() as guest:
            cursor = guest.open_tree("/lib/modules/6.12.0", limit=1)
            with pytest.raises(OSError, match="mkfifo setup") as raised:
                cursor.__enter__()

        assert raised.value.__notes__ == [
            "guest-tree cursor cleanup failed: OSError('rmdir cleanup')"
        ]
        session.close()
    finally:
        if directory.exists():
            original_rmdir(directory)


def test_guest_context_closes_caller_abandoned_find0_cursor() -> None:
    events: list[str] = []
    producer = Find0Guest(events, [], wait_before_write=True)
    session, _lease_value = _stream_session(events, producer)

    with session.guest() as guest:
        cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
        cursor.__enter__()
        assert producer.producer_started.wait(timeout=5)

    assert producer.cancel_requested.is_set()
    assert producer.producer_finished.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    session.close()


def test_find0_tree_backend_error_is_reported_after_cleanup() -> None:
    events: list[str] = []
    producer = Find0Guest(events, [b"a"], producer_fault=LookupError("find0 backend"))
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(LookupError, match="find0 backend"),
        guest.open_tree("/lib/modules/6.12.0", limit=2) as entries,
    ):
        list(entries)

    assert producer.cancel_requested.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    session.close()


def test_find0_tree_preserves_backend_error_when_cleanup_fails() -> None:
    events: list[str] = []

    class CleanupFaultGuest(Find0Guest):
        def user_cancel(self) -> None:
            super().user_cancel()
            raise OSError("cancel secondary")

    producer = CleanupFaultGuest(
        events,
        [b"a"],
        producer_fault=LookupError("find0 backend"),
    )
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(LookupError, match="find0 backend") as raised,
        guest.open_tree("/lib/modules/6.12.0", limit=2) as entries,
    ):
        list(entries)

    assert raised.value.__notes__ == [
        "guest-tree cursor cleanup failed: OSError('cancel secondary')"
    ]
    session.close()


def test_find0_tree_reports_backend_eintr_without_deliberate_early_close() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [b"a"],
        producer_fault=OSError(errno.EINTR, "unexpected find0 interruption"),
    )
    session, _lease_value = _stream_session(events, producer)

    with (
        session.guest() as guest,
        pytest.raises(InterruptedError, match="unexpected find0 interruption"),
        guest.open_tree("/lib/modules/6.12.0", limit=2) as entries,
    ):
        list(entries)

    assert producer.cancel_requested.is_set()
    assert producer.output_path is not None
    assert not producer.output_path.parent.exists()
    session.close()


def test_session_close_retains_pin_until_noncooperating_find0_producer_exits() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    session, _lease_value = _stream_session(events, producer, lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
    cursor.__enter__()
    assert producer.producer_started.wait(timeout=5)
    close_finished = threading.Event()
    close_error: list[BaseException] = []

    def close_session() -> None:
        try:
            session.close()
        except BaseException as exc:
            close_error.append(exc)
        finally:
            close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    assert producer.cancel_requested.wait(timeout=5)
    assert not close_finished.is_set()
    assert lease.pins == 1
    assert producer.output_path is not None
    assert producer.output_path.parent.exists()

    producer.release_producer.set()
    assert close_finished.wait(timeout=5)
    closer.join()
    assert close_error == []
    assert lease.pins == 0
    assert not producer.output_path.parent.exists()


def test_cursor_close_racing_session_close_keeps_producer_owned_until_join() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    pin_lease, pin_observations = _recording_pin_lease(events, producer, lease)
    session, _lease_value = _stream_session(events, producer, lease, pin_lease=pin_lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
    cursor.__enter__()
    assert producer.producer_started.wait(timeout=5)
    lifecycle_lock = _instrument_lifecycle_lock(session)
    session_close_attempted = lifecycle_lock.expect_attempt("session-close")
    cursor_closer, cursor_close_finished, cursor_errors = _start_call(
        cursor.close, name="cursor-close"
    )
    assert producer.cancel_requested.wait(timeout=5)
    session_closer, session_close_finished, session_errors = _start_call(
        session.close, name="session-close"
    )
    try:
        assert session_close_attempted.wait(timeout=5)
        assert not session_close_finished.is_set()
        assert not cursor_close_finished.is_set()
        assert lease.pins == 1
        assert not producer.producer_finished.is_set()
        assert events.count("guest.close") == 0
        assert producer.output_path is not None
        assert producer.output_path.parent.exists()
    finally:
        producer.release_producer.set()
        cursor_closer.join(timeout=5)
        session_closer.join(timeout=5)

    assert cursor_errors == []
    assert session_errors == []
    assert cursor_close_finished.is_set()
    assert session_close_finished.is_set()
    assert events.count("guest.cancel") == 1
    assert events.count("guest.shutdown") == 1
    assert events.count("guest.close") == 1
    assert events.count("artifact.close") == 1
    assert events.count("overlay.close") == 1
    assert events.count("domain.close") == 1
    assert events.count("connection.close") == 1
    assert events.count("pin.close") == 1
    assert pin_observations == [(True, 1, False)]
    assert lease.pins == 0
    assert events[-1] == "pin.close"


def test_guest_close_racing_session_close_keeps_producer_owned_until_join() -> None:
    events: list[str] = []
    producer = Find0Guest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    pin_lease, pin_observations = _recording_pin_lease(events, producer, lease)
    session, _lease_value = _stream_session(events, producer, lease, pin_lease=pin_lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
    cursor.__enter__()
    assert producer.producer_started.wait(timeout=5)
    lifecycle_lock = _instrument_lifecycle_lock(session)
    session_close_attempted = lifecycle_lock.expect_attempt("session-close")

    def close_guest() -> None:
        guest_context.__exit__(None, None, None)

    guest_closer, guest_close_finished, guest_errors = _start_call(close_guest, name="guest-close")
    assert producer.cancel_requested.wait(timeout=5)
    session_closer, session_close_finished, session_errors = _start_call(
        session.close, name="session-close"
    )
    try:
        assert session_close_attempted.wait(timeout=5)
        assert not session_close_finished.is_set()
        assert not guest_close_finished.is_set()
        assert lease.pins == 1
        assert not producer.producer_finished.is_set()
        assert events.count("guest.close") == 0
        assert producer.output_path is not None
        assert producer.output_path.parent.exists()
    finally:
        producer.release_producer.set()
        guest_closer.join(timeout=5)
        session_closer.join(timeout=5)

    assert guest_errors == []
    assert session_errors == []
    assert guest_close_finished.is_set()
    assert session_close_finished.is_set()
    assert events.count("guest.cancel") == 1
    assert events.count("guest.shutdown") == 1
    assert events.count("guest.close") == 1
    assert events.count("artifact.close") == 1
    assert events.count("overlay.close") == 1
    assert events.count("domain.close") == 1
    assert events.count("connection.close") == 1
    assert events.count("pin.close") == 1
    assert pin_observations == [(True, 1, False)]
    assert lease.pins == 0
    assert events[-1] == "pin.close"


def test_repeated_cursor_close_waits_for_faulting_teardown_owner() -> None:
    events: list[str] = []

    class CancelFaultGuest(Find0Guest):
        cancel_calls = 0

        def user_cancel(self) -> None:
            self.cancel_calls += 1
            super().user_cancel()
            raise OSError("cancel fault")

    producer = CancelFaultGuest(
        events,
        [],
        wait_before_write=True,
        cooperate_with_cancel=False,
    )
    lease = _lease()
    session, _lease_value = _stream_session(events, producer, lease)
    guest_context = session.guest()
    guest = guest_context.__enter__()
    cursor = guest.open_tree("/lib/modules/6.12.0", limit=2)
    cursor.__enter__()
    assert producer.producer_started.wait(timeout=5)
    lifecycle_lock = _instrument_lifecycle_lock(session)
    second_close_attempted = lifecycle_lock.expect_attempt("second-cursor-close")
    session_close_attempted = lifecycle_lock.expect_attempt("session-close")
    first_closer, first_finished, first_errors = _start_call(
        cursor.close, name="first-cursor-close"
    )
    assert producer.cancel_requested.wait(timeout=5)
    second_closer, second_finished, second_errors = _start_call(
        cursor.close, name="second-cursor-close"
    )
    session_closer, session_finished, session_errors = _start_call(
        session.close, name="session-close"
    )
    try:
        assert second_close_attempted.wait(timeout=5)
        assert session_close_attempted.wait(timeout=5)
        assert not first_finished.is_set()
        assert not second_finished.is_set()
        assert not session_finished.is_set()
        assert lease.pins == 1
        assert not producer.producer_finished.is_set()
        assert events.count("guest.close") == 0
    finally:
        producer.release_producer.set()
        first_closer.join(timeout=5)
        second_closer.join(timeout=5)
        session_closer.join(timeout=5)

    assert len(first_errors) == 1
    assert isinstance(first_errors[0], OSError)
    assert str(first_errors[0]) == "cancel fault"
    assert second_errors == []
    assert session_errors == []
    assert first_finished.is_set()
    assert second_finished.is_set()
    assert session_finished.is_set()
    assert producer.cancel_calls == 1
    assert events.count("guest.shutdown") == 1
    assert events.count("guest.close") == 1
    assert lease.pins == 0


def test_guest_regular_stream_transfer_exposes_no_host_path_and_closes_on_success() -> None:
    events: list[str] = []

    class StreamGuest(Guest):
        def download(self, remotefilename: str, filename: str) -> None:
            self.events.append(f"guest.download:{remotefilename}")
            with open(filename, "wb") as destination:
                destination.write(b"elf")

        def upload(self, filename: str, remotefilename: str) -> None:
            with open(filename, "rb") as source:
                self.events.append(f"guest.upload:{remotefilename}:{source.read()!r}")

    stream_guest = StreamGuest(events)
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: stream_guest,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())

    with session.guest() as guest:
        with guest.open_regular("/lib/modules/6.12.0/a.ko", size=3) as content:
            assert content.read() == b"elf"
        guest.create_regular(io.BytesIO(b"new"), "/lib/modules/staging/a.ko", size=3)

    assert "guest.download:/lib/modules/6.12.0/a.ko" in events
    assert "guest.upload:/lib/modules/staging/a.ko:b'new'" in events
    assert not any("/tmp/" in event for event in events)
    session.close()


def test_guest_regular_stream_short_input_rejects_before_guest_write() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease(), _expected())

    with session.guest() as guest, pytest.raises(ValueError, match="ended before"):
        guest.create_regular(io.BytesIO(b"x"), "/lib/modules/staging/a.ko", size=2)

    assert not any(event.startswith("upload:") for event in events)
    session.close()


def test_concrete_find0_orders_produce_identical_recovery_identity(tmp_path: Path) -> None:
    class RecoveryGuest(Find0Guest):
        def lstatns(self, path: str) -> dict[str, int]:
            directory = path.endswith("/kernel")
            return {
                "st_mode": (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o600),
                "st_uid": 0,
                "st_gid": 0,
                "st_size": 0 if directory else 3,
                "st_nlink": 1,
            }

        def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
            del path
            return []

        def download(self, remotefilename: str, filename: str) -> None:
            del remotefilename
            with open(filename, "wb") as destination:
                destination.write(b"elf")

    captures: list[ModuleArchiveCapture] = []
    for name, order in (
        ("first", [b"z.ko", b"kernel", b"kernel/a.ko"]),
        ("second", [b"kernel/a.ko", b"z.ko", b"kernel"]),
    ):
        events: list[str] = []
        producer = RecoveryGuest(events, order)
        session, _lease_value = _stream_session(events, producer)
        archive = tmp_path / name
        archive.mkdir(mode=0o700)
        descriptor = os.open(archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            sink = RecoveryArchiveSink(descriptor, binding=BINDING, release="6.12.0")
        finally:
            os.close(descriptor)
        with session.guest() as guest:
            tree = LibguestfsAuthenticatedGuestTree(
                guest,
                binding=BINDING,
                release="6.12.0",
                root="/lib/modules/6.12.0",
                mutable=False,
            )
            capture = RealGuestRecoveryWriter().capture(tree, "6.12.0", sink)
        assert isinstance(capture, ModuleArchiveCapture)
        captures.append(capture)
        session.close()

    assert captures[0].manifest == captures[1].manifest
    assert captures[0].archive_sha256 == captures[1].archive_sha256


def _factory(
    events: list[str],
    domain: Domain | None = None,
    *,
    pin_lease: PinOperationLease = LANE.pin,
) -> LocalExternalBootSessionFactory:
    selected = domain or Domain(events)
    return LocalExternalBootSessionFactory(
        connect=lambda: events.append("connection.open") or Conn(events, selected),
        pin_lease=pin_lease,
        open_artifact_root=lambda _lease: events.append("artifact.open") or 41,
        open_guest=lambda: events.append("guest.open") or Guest(events),
        worker_pid=4242,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )


def test_factory_pins_before_open_and_lease_cannot_release_while_session_live() -> None:
    events: list[str] = []
    lease = _lease()
    session = _factory(events).open(lease, _expected())
    assert events[:2] == ["connection.open", f"domain.open:kdive-{SYSTEM_ID}"]
    with pytest.raises(RuntimeError, match="pinned"):
        lease.release()
    session.close()
    lease.release()


@pytest.mark.parametrize("lease", [None, object()])
def test_missing_or_foreign_lease_opens_nothing(lease: object | None) -> None:
    events: list[str] = []
    with pytest.raises((TypeError, ValueError)):
        _factory(events).open(lease, _expected())  # ty: ignore[invalid-argument-type]
    assert events == []


def test_released_lease_opens_nothing() -> None:
    events: list[str] = []
    lease = _lease()
    lease.release()
    events.clear()
    with pytest.raises(RuntimeError, match="released"):
        _factory(events).open(lease, _expected())
    assert events == []


def test_inspection_is_exact_immutable_and_validates_ownership() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease(), _expected())
    inspection = session.inspect_closed()
    assert inspection.xml == _xml().encode()
    assert inspection.domain_name == f"kdive-{SYSTEM_ID}"
    assert inspection.overlay.device == 8 and inspection.overlay.inode == 9
    assert inspection.definition_identity.startswith("sha256:")
    assert inspection.source_boot_identity.startswith("sha256:")
    assert inspection.definition_identity == (
        "sha256:b3a319e84c14ad49042057dab9c8ce445a144e7d4dea7b193674d9a35aef3110"
    )
    assert inspection.source_boot_identity == (
        "sha256:af7c3d00f78226cd4b917aa70737e2557cfa5989c5efa9a42b2555992adbe176"
    )
    with pytest.raises(FrozenInstanceError):
        inspection.active = True  # ty: ignore[invalid-assignment]
    session.close()

    foreign = Domain(events, _xml(system_id=UUID(int=4)))
    with pytest.raises(ValueError, match="ownership"):
        _factory(events, foreign).open(_lease(), _expected())


@pytest.mark.parametrize(
    ("xml", "reason"),
    [
        (_xml().replace('disk type="file"', 'disk type="block"'), "overlay"),
        (_xml().replace("</disk>", "<readonly/></disk>"), "overlay"),
        (_xml().replace('dev="vda"', 'dev="sdb"'), "overlay"),
        (_xml().replace('bus="virtio"', 'bus="scsi"'), "overlay"),
    ],
)
def test_domain_rejects_noncanonical_or_readonly_overlay(xml: str, reason: str) -> None:
    events: list[str] = []
    with pytest.raises(ValueError, match=reason):
        _factory(events, Domain(events, xml)).open(_lease(), _expected())


def test_guest_fences_and_rechecks_overlay_and_can_reopen() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease(), _expected())
    with session.guest() as guest:
        assert guest.exists("/etc/os-release") == 1
    assert events.index("guest.launch") < events.index("guest.inspect")
    assert events.index("guest.inspect") < events.index("guest.mount:/dev/sda1:/")
    assert events.index("guest.mount:/dev/sda1:/") < events.index("guest.exists:/etc/os-release")
    assert not hasattr(guest, "find")
    with session.guest() as guest:
        assert guest.exists("/etc/os-release") == 1
    assert events.count("guest.open") == 2
    assert events.index("domain.active") < events.index("guest.drive:/proc/4242/fd/40:qcow2")
    session.close()


@pytest.mark.parametrize("roots", [[], ["/dev/sda1", "/dev/sda2"]])
def test_guest_rejects_zero_or_ambiguous_inspection_roots(roots: list[str]) -> None:
    events: list[str] = []

    class RootGuest(Guest):
        def inspect_os(self) -> list[str]:
            self.events.append("guest.inspect")
            return roots

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: RootGuest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    with pytest.raises(RuntimeError, match="exactly one operating-system root"):
        session.guest().__enter__()
    assert "guest.shutdown" in events
    assert "guest.close" in events
    assert not any(event.startswith("guest.mount:") for event in events)
    session.close()


@pytest.mark.parametrize("fault_at", ["inspect", "mount"])
def test_guest_inspection_or_mount_failure_preserves_primary_and_cleans_up(fault_at: str) -> None:
    events: list[str] = []

    class MountFaultGuest(Guest):
        def inspect_os(self) -> list[str]:
            roots = super().inspect_os()
            if fault_at == "inspect":
                raise LookupError("inspect primary")
            return roots

        def mount(self, device: str, mountpoint: str) -> None:
            super().mount(device, mountpoint)
            if fault_at == "mount":
                raise LookupError("mount primary")

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: MountFaultGuest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    with pytest.raises(LookupError, match=f"{fault_at} primary"):
        session.guest().__enter__()
    assert events[-2:] == ["guest.shutdown", "guest.close"]
    session.close()


def test_guest_rechecks_inactive_and_overlay_before_every_operation() -> None:
    events: list[str] = []
    domain = Domain(events)
    stats = [(8, 9)]
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, domain),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (*stats[-1], stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    with session.guest() as guest:
        domain.active = True
        before = events.count("guest.exists:/etc/os-release")
        with pytest.raises(RuntimeError, match="inactive"):
            guest.exists("/etc/os-release")
        assert events.count("guest.exists:/etc/os-release") == before
        domain.active = False
        stats.append((8, 10))
        with pytest.raises(ValueError, match="overlay changed"):
            guest.exists("/etc/os-release")
        assert events.count("guest.exists:/etc/os-release") == before
    session.close()


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/b", ".", "..", ""])
def test_guest_artifact_transfer_rejects_host_path_escape_before_open(name: str) -> None:
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda *_args: events.append("artifact.child.open") or 42,
        close_transfer_descriptor=lambda _fd: events.append("artifact.child.close"),
    )
    session = factory.open(_lease(), _expected())
    with session.guest() as guest:
        assert not hasattr(guest, "upload")
        assert not hasattr(guest, "download")
        with pytest.raises(ValueError, match="canonical relative"):
            guest.upload_artifact(name, "/guest/destination")
        with pytest.raises(ValueError, match="canonical relative"):
            guest.download_artifact("/guest/source", name)
    assert "artifact.child.open" not in events
    session.close()


def test_guest_artifact_transfers_use_owned_fds_and_close_each() -> None:
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda root, name, flags, mode: (
            events.append(f"artifact.child.open:{root}:{name}:{flags}:{mode}") or 42
        ),
        close_transfer_descriptor=lambda fd: events.append(f"artifact.child.close:{fd}"),
        fsync_descriptor=lambda fd: events.append(f"artifact.child.fsync:{fd}"),
        replace_relative=lambda root, source, target: events.append(
            f"artifact.replace:{root}:{source}:{target}"
        ),
        temporary_artifact_name=lambda name: f".{name}.tmp",
    )
    session = factory.open(_lease(), _expected())
    with session.guest() as guest:
        guest.upload_artifact("input.tar", "/guest/input.tar")
        guest.download_artifact("/guest/output.tar", "output.tar")
    assert "upload:/proc/self/fd/42:/guest/input.tar" in events
    assert "download:/guest/output.tar:/proc/self/fd/42" in events
    assert events.count("artifact.child.close:42") == 2
    session.close()


def test_download_publishes_atomically_after_fsync_and_close() -> None:
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda root, name, flags, mode: (
            events.append(f"open:{root}:{name}:{flags}:{mode}") or 42
        ),
        close_transfer_descriptor=lambda fd: events.append(f"close:{fd}"),
        fsync_descriptor=lambda fd: events.append(f"fsync:{fd}"),
        replace_relative=lambda root, source, target: events.append(
            f"replace:{root}:{source}:{target}"
        ),
        temporary_artifact_name=lambda name: f".{name}.tmp",
    )
    session = factory.open(_lease(), _expected())
    with session.guest() as guest:
        guest.download_artifact("/guest/output.tar", "output.tar")
    open_event = next(event for event in events if event.startswith("open:"))
    assert f":{os.O_WRONLY | os.O_CREAT | os.O_EXCL}:" in open_event
    assert events.index("download:/guest/output.tar:/proc/self/fd/42") < events.index("fsync:42")
    assert events.index("fsync:42") < events.index("close:42")
    assert events.index("close:42") < events.index("replace:41:.output.tar.tmp:output.tar")
    session.close()


def test_failed_download_removes_temp_and_preserves_existing_final() -> None:
    events: list[str] = []

    class PartialDownloadGuest(Guest):
        def download(self, remotefilename: str, filename: str) -> None:
            super().download(remotefilename, filename)
            raise OSError("partial download")

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: PartialDownloadGuest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda *_args: 42,
        close_transfer_descriptor=lambda fd: events.append(f"close:{fd}"),
        fsync_descriptor=lambda fd: events.append(f"fsync:{fd}"),
        replace_relative=lambda root, source, target: events.append(
            f"replace:{root}:{source}:{target}"
        ),
        unlink_relative=lambda root, name: events.append(f"unlink:{root}:{name}"),
        temporary_artifact_name=lambda name: f".{name}.tmp",
    )
    session = factory.open(_lease(), _expected())
    with session.guest() as guest, pytest.raises(OSError, match="partial download"):
        guest.download_artifact("/guest/output.tar", "output.tar")
    assert "close:42" in events
    assert "unlink:41:.output.tar.tmp" in events
    assert not any(event.startswith("replace:") for event in events)
    session.close()


def test_guest_transfer_preserves_primary_and_attempts_faulting_fd_close() -> None:
    events: list[str] = []

    class TransferFaultGuest(Guest):
        def upload(self, filename: str, remotefilename: str) -> None:
            super().upload(filename, remotefilename)
            raise LookupError("transfer primary")

    def close_fault(fd: int) -> None:
        events.append(f"artifact.child.close:{fd}")
        raise OSError("close secondary")

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: TransferFaultGuest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda *_args: 42,
        close_transfer_descriptor=close_fault,
    )
    session = factory.open(_lease(), _expected())
    with (
        session.guest() as guest,
        pytest.raises(LookupError, match="transfer primary") as raised,
    ):
        guest.upload_artifact("input.tar", "/guest/input.tar")
    assert raised.value.__notes__ == ["cleanup failed: OSError('close secondary')"]
    assert events.count("artifact.child.close:42") == 1
    session.close()


def test_only_one_guest_context_and_power_start_reject_while_open() -> None:
    events: list[str] = []
    domain = Domain(events)
    session = _factory(events, domain).open(_lease(), _expected())
    first = session.guest()
    first.__enter__()
    guest_opens = events.count("guest.open")
    with pytest.raises(RuntimeError, match="already open"):
        session.guest().__enter__()
    assert events.count("guest.open") == guest_opens
    with pytest.raises(RuntimeError, match="guest context"):
        session.start()
    with pytest.raises(RuntimeError, match="guest context"):
        session.restore_power("running")
    defines = events.count("domain.define")
    closes = events.count("domain.close")
    with pytest.raises(RuntimeError, match="guest context"):
        session.define_xml(_xml())
    assert events.count("domain.define") == defines
    assert events.count("domain.close") == closes
    assert "domain.create" not in events
    first.__exit__(None, None, None)
    with session.guest():
        pass
    session.close()


def test_close_poisons_wrappers_and_releases_pin_last() -> None:
    events: list[str] = []
    lease = _lease()
    session = _factory(events).open(lease, _expected())
    retained = session.guest()
    guest = retained.__enter__()
    session.close()
    assert events[-5:] == [
        "guest.shutdown",
        "guest.close",
        "artifact.close",
        "domain.close",
        "connection.close",
    ]
    lease.release()
    for call in (session.inspect_closed, lambda: guest.exists("/etc/os-release")):
        with pytest.raises(RuntimeError, match="closed"):
            call()


def test_close_faults_do_not_skip_cleanup_or_pin_release() -> None:
    events: list[str] = []

    class FaultingDomain(Domain):
        def free(self) -> None:
            super().free()
            raise OSError("domain close")

    class FaultingConn(Conn):
        def close(self) -> None:
            super().close()
            raise OSError("connection close")

    lease = _lease()
    domain = FaultingDomain(events)

    def fault_overlay_close(_fd: int) -> None:
        events.append("overlay.close")
        raise OSError("overlay close")

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: FaultingConn(events, domain),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=fault_overlay_close,
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease, _expected())
    with pytest.raises(ExceptionGroup) as raised:
        session.close()
    assert len(raised.value.exceptions) == 3
    assert events[-4:] == [
        "artifact.close",
        "overlay.close",
        "domain.close",
        "connection.close",
    ]
    lease.release()


def test_active_domain_blocks_guest_before_open() -> None:
    events: list[str] = []
    domain = Domain(events)
    domain.active = True
    session = _factory(events, domain).open(_lease(), _expected())
    with pytest.raises(RuntimeError, match="inactive"), session.guest():
        pass
    assert "guest.open" not in events
    session.close()


def test_overlay_substitution_fails_before_guest_open() -> None:
    events: list[str] = []
    stats = iter(((8, 9), (8, 10)))
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: events.append("guest.open") or Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (*next(stats), stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    with pytest.raises(ValueError, match="overlay changed"), session.guest():
        pass
    assert "guest.open" not in events
    session.close()


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o700])
def test_overlay_descriptor_rejects_symlink_and_nonregular_before_guest(mode: int) -> None:
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: events.append("guest.open") or Guest(events),
        open_overlay=lambda _path: events.append("overlay.open") or 40,
        fstat_overlay=lambda _fd: (8, 9, mode),
        close_overlay_descriptor=lambda _fd: events.append("overlay.close"),
        close_descriptor=lambda _fd: None,
    )
    with pytest.raises(ValueError, match="regular"):
        factory.open(_lease(), _expected())
    assert "guest.open" not in events
    assert events[-3:] == ["overlay.close", "domain.close", "connection.close"]


def test_guest_attaches_retained_overlay_fd_despite_path_replacement() -> None:
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _ownership: 41,
        open_guest=lambda: Guest(events),
        worker_pid=4242,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda fd: events.append(f"overlay.close:{fd}"),
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    with session.guest():
        pass
    assert "guest.drive:/proc/4242/fd/40:qcow2" in events
    assert "guest.drive:/proc/self/fd/40:qcow2" not in events
    assert f"guest.drive:{OVERLAY}:qcow2" not in events
    session.close()
    assert "overlay.close:40" in events


def test_partial_construction_closes_every_acquired_resource_and_pin_last() -> None:
    events: list[str] = []
    domain = Domain(events, _xml(overlay="/wrong.qcow2"))
    lease = _lease()
    with pytest.raises(ValueError, match="overlay"):
        _factory(events, domain).open(lease, _expected())
    assert events[-2:] == ["domain.close", "connection.close"]
    lease.release()


def test_narrow_injected_primitives_keep_host_authority_private() -> None:
    events: list[str] = []
    domain = Domain(events)
    observation = RunningKernelObservation(
        architecture="x86_64", release="6.1.0", gnu_build_id="00112233"
    )
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, domain),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        open_relative=lambda root, name, flags, mode: (
            events.append(f"openat:{root}:{name}:{flags}:{mode}") or 42
        ),
        unlink_relative=lambda root, name: events.append(f"unlinkat:{root}:{name}"),
        readiness=lambda _system_id: ReadinessResult(True, True),
        observe_running=lambda _system_id: observation,
        cleanup_payloads=lambda root, binding: events.append(
            f"cleanup:{root}:{binding.activation_id}"
        ),
    )
    session = factory.open(_lease(), _expected())
    assert session.open_artifact("point.json", 0) == 42
    session.unlink_artifact("point.json")
    assert session.readiness() == ReadinessResult(True, True)
    assert session.observe_running() == observation
    session.cleanup_payloads()
    session.restore_power("running")
    assert domain.active
    session.restore_power("inactive")
    assert not domain.active
    assert not hasattr(session.inspect_closed().overlay, "path")
    assert not hasattr(session, "artifact_root_descriptor")
    with pytest.raises(ValueError, match="relative"):
        session.open_artifact("../escape", 0)
    session.close()
    for call in (
        session.readiness,
        session.observe_running,
        lambda: session.restore_power("running"),
        session.cleanup_payloads,
    ):
        with pytest.raises(RuntimeError, match="closed"):
            call()


def test_session_snapshots_ownership_after_lane_pin() -> None:
    events: list[str] = []
    observed_ids: list[UUID] = []
    cleaned: list[ExternalBootActivationBinding] = []
    lease = _lease()
    original_binding = lease.binding
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
        readiness=lambda system_id: observed_ids.append(system_id) or ReadinessResult(True, True),
        observe_running=lambda system_id: (
            observed_ids.append(system_id)
            or RunningKernelObservation(
                architecture="x86_64", release="6.1.0", gnu_build_id="00112233"
            )
        ),
        cleanup_payloads=lambda _root, binding: cleaned.append(binding),
    )
    session = factory.open(lease, _expected())
    lease.system_id = UUID(int=9)
    lease.binding = ExternalBootActivationBinding(
        system_id=str(UUID(int=9)),
        run_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        activation_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    assert session.inspect_closed().domain_name == f"kdive-{SYSTEM_ID}"
    session.readiness()
    session.observe_running()
    session.cleanup_payloads()
    assert observed_ids == [SYSTEM_ID, SYSTEM_ID]
    assert cleaned == [original_binding]
    session.close()


def test_pinner_mutation_cannot_change_atomic_ownership_snapshot() -> None:
    events: list[str] = []
    lease = _lease()

    def pin_then_mutate(candidate: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        assert isinstance(candidate, FakeLease)
        ownership = PinnedOperationOwnership(
            OperationOwnership(candidate.system_id, candidate.binding), FakePin(candidate)
        )
        candidate.system_id = UUID(int=9)
        candidate.binding = ExternalBootActivationBinding(
            system_id=str(UUID(int=9)),
            run_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            activation_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        return ownership

    factory = LocalExternalBootSessionFactory(
        pin_lease=pin_then_mutate,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda ownership: (
            events.append(f"artifact-owner:{ownership.system_id}:{ownership.binding.activation_id}")
            or 41
        ),
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(lease, _expected())
    assert session.inspect_closed().domain_name == f"kdive-{SYSTEM_ID}"
    assert events.count(f"domain.open:kdive-{SYSTEM_ID}") == 1
    assert events.count(f"artifact-owner:{SYSTEM_ID}:{BINDING.activation_id}") == 1
    session.close()


def test_artifact_callback_cannot_redirect_snapshot_by_mutating_caller_lease() -> None:
    events: list[str] = []
    lease = _lease()
    received: list[OperationOwnership] = []

    def mutate_during_artifact(ownership: OperationOwnership) -> int:
        lease.system_id = UUID(int=9)
        lease.binding = ExternalBootActivationBinding(
            system_id=str(UUID(int=9)),
            run_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            activation_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        received.append(ownership)
        return 41

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=mutate_during_artifact,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(lease, _expected())
    assert received[0].system_id == SYSTEM_ID
    assert received[0].binding == BINDING
    assert session.inspect_closed().domain_name == f"kdive-{SYSTEM_ID}"
    session.close()


def test_artifact_callback_type_is_pin_free_and_cannot_release_lane() -> None:
    received: list[OperationOwnership] = []

    def open_root(ownership: OperationOwnership) -> int:
        received.append(ownership)
        assert not hasattr(ownership, "pin")
        assert not hasattr(ownership, "_pin")
        assert not hasattr(ownership, "close")
        return 41

    callback: OpenArtifactRoot = open_root
    lease = _lease()
    events: list[str] = []
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=callback,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(lease, _expected())
    assert received == [OperationOwnership(SYSTEM_ID, BINDING)]
    with pytest.raises(RuntimeError, match="pinned"):
        lease.release()
    session.close()
    lease.release()


def test_define_frees_distinct_prior_domain_reference() -> None:
    events: list[str] = []
    prior = Domain(events)
    replacement = Domain(events)

    class ReplacingConn(Conn):
        def defineXML(self, xml: str) -> Domain:  # noqa: N802
            self.events.append("domain.define")
            replacement.xml = xml
            return replacement

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: ReplacingConn(events, prior),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease(), _expected())
    session.define_xml(_xml())
    assert events[-2:] == ["domain.define", "domain.close"]
    session.close()
    assert events.count("domain.close") == 2


def test_guest_and_descriptor_close_faults_still_release_pin_last() -> None:
    events: list[str] = []

    class FaultingGuest(Guest):
        def close(self) -> None:
            super().close()
            raise OSError("guest close")

    def fault_descriptor(_fd: int) -> None:
        raise OSError("descriptor close")

    lease = _lease()
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: FaultingGuest(events),
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=fault_descriptor,
    )
    session = factory.open(lease, _expected())
    retained = session.guest()
    retained.__enter__()
    with pytest.raises(ExceptionGroup) as raised:
        session.close()
    assert len(raised.value.exceptions) == 2
    assert events[-2:] == ["domain.close", "connection.close"]
    lease.release()


@pytest.mark.parametrize("fault_at", ["factory", "drive", "launch"])
def test_guest_open_failures_preserve_primary_and_cleanup(fault_at: str) -> None:
    events: list[str] = []
    lease = _lease()

    class LaunchFaultGuest(Guest):
        def add_drive_opts(self, overlay: str, *, format: str) -> None:
            super().add_drive_opts(overlay, format=format)
            if fault_at == "drive":
                raise LookupError("drive primary")

        def launch(self) -> None:
            self.events.append("guest.launch")
            if fault_at == "launch":
                raise LookupError("launch primary")

    def open_guest() -> Guest:
        events.append("guest.open")
        if fault_at == "factory":
            raise LookupError("factory primary")
        return LaunchFaultGuest(events)

    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: Conn(events, Domain(events)),
        open_artifact_root=lambda _lease: 41,
        open_guest=open_guest,
        open_overlay=lambda _path: 40,
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=lambda _fd: None,
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease, _expected())
    with pytest.raises(LookupError, match=f"{fault_at} primary"), session.guest():
        pass
    if fault_at != "factory":
        assert events[-2:] == ["guest.shutdown", "guest.close"]
    with pytest.raises(RuntimeError, match="pinned"):
        lease.release()
    session.close()
    lease.release()
