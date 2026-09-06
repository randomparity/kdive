from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

import libvirt

from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    ApplianceRequest,
    ApplianceStream,
    UnresolvedCallError,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    RemoteDeviceIdentity,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import PreparedVolume
from kdive.security.secrets.secret_registry import SecretRegistry

UUID1 = "12345678-1234-4123-8123-123456789abc"

UUID2 = "22345678-1234-4123-8123-123456789abc"

DIGEST = "sha256:" + "a" * 64

MANIFEST = "sha256:" + "b" * 64


def libvirt_error(code: int) -> libvirt.libvirtError:
    error = libvirt.libvirtError("synthetic")
    error.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return error


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class Executor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        return operation()


class DeadlineAwareExecutor:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.abort_deadline: float | None = None
        self.deadlines: list[float] = []

    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        self.deadlines.append(deadline)
        if deadline <= self.clock():
            raise TimeoutError
        if getattr(operation, "__name__", None) == "abort":
            self.abort_deadline = deadline
        return operation()


class Stream:
    def __init__(self, chunks: Sequence[bytes | int | None], clock: Clock) -> None:
        self.chunks = list(chunks)
        self.clock = clock
        self.aborted = False

    def recv(self, size: int) -> bytes | int | None:
        self.clock.value += 1
        return self.chunks.pop(0) if self.chunks else b""

    def abort(self) -> int:
        self.aborted = True
        return 0


class Domain:
    def __init__(self, xml: str, *, active: bool = True, persistent: bool = False) -> None:
        self.xml = xml
        self.active = active
        self.persistent = persistent
        self.destroyed = False

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return int(self.persistent)

    def openConsole(  # noqa: N802
        self, device: str | None, stream: ApplianceStream, flags: int
    ) -> int:
        return 0

    def destroy(self) -> int:
        self.destroyed = True
        return 0


class Conn:
    def __init__(
        self,
        chunks: Sequence[bytes | int | None],
        clock: Clock,
        *,
        existing: Domain | None = None,
        lookup_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.chunks = chunks
        self.clock = clock
        self.domain = existing
        self.lookup_error = lookup_error
        self.created_flags: int | None = None
        self.stream: Stream | None = None

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        if self.lookup_error is not None:
            raise self.lookup_error
        if self.domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.domain

    def createXML(self, xml: str, flags: int = 0) -> Domain:  # noqa: N802
        self.created_flags = flags
        self.domain = Domain(xml)
        return self.domain

    def newStream(self, flags: int = 0) -> Stream:  # noqa: N802
        self.stream = Stream(self.chunks, self.clock)
        return self.stream


class FailingDestroyDomain(Domain):
    def destroy(self) -> int:
        raise RuntimeError("destroy failed")


class FailingDestroyConn(Conn):
    def createXML(self, xml: str, flags: int = 0) -> Domain:  # noqa: N802
        self.created_flags = flags
        self.domain = FailingDestroyDomain(xml)
        return self.domain


class FailingOpenDomain(Domain):
    def __init__(self, xml: str, failure: Exception) -> None:
        super().__init__(xml)
        self.failure = failure

    def openConsole(  # noqa: N802
        self, device: str | None, stream: ApplianceStream, flags: int
    ) -> int:
        raise self.failure


def operation() -> RemoteModuleOperationV1:
    return RemoteModuleOperationV1(
        operation="capture_install",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        release="6.8.0-test",
        root_volume={"key": "root", "identity": DIGEST},
        source_manifest=MANIFEST,
        appliance_image_digest=DIGEST,
    )


def success_result() -> bytes:
    return RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()


def volume(name: str, purpose: str) -> PreparedVolume:
    return PreparedVolume("pool", name, UUID1, UUID2, "1" * 32, purpose, DIGEST, 4096)


def request(clock: Clock, *, result: bytes | None = None) -> ApplianceRequest:
    registry = SecretRegistry()
    registry.register("split-secret", scope=None)
    return ApplianceRequest(
        name="kdive-module-12345678-11111111",
        architecture="x86_64",
        emulator_path="/usr/bin/qemu-system-x86_64",
        memory_kib=262_144,
        vcpus=1,
        pool="pool",
        appliance_volume="appliance-x86_64.qcow2",
        appliance_image_digest=DIGEST,
        root=volume("root", "root"),
        source=volume("source", "source"),
        scratch=volume("scratch", "scratch"),
        operation=operation(),
        secret_registry=registry,
        read_scratch_result=lambda: result,
        inspect_attachments=lambda: AttachmentInspection(
            True,
            True,
            False,
            frozenset({("pool", "source"), ("pool", "scratch")}),
        ),
        executor=Executor(),
        monotonic=clock,
    )


class TimingOutExecutor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        raise TimeoutError


class ThreadDeadlineExecutor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        done = threading.Event()
        result: list[T] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(operation())
            except BaseException as exc:  # test executor must transport worker failures
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=invoke, daemon=True).start()
        if not done.wait(0.01):
            raise UnresolvedCallError
        if errors:
            raise errors[0]
        return result[0]


class BlockingConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.release = release

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        self.release.wait()
        return super().lookupByName(name)


class BlockingStream(Stream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.release = release

    def recv(self, size: int) -> bytes | int | None:
        self.release.wait()
        return b""


class BlockingConsoleConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.blocking_stream = BlockingStream(release, clock)

    def newStream(self, flags: int = 0) -> BlockingStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


class BlockingAbortStream(Stream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([-2] * 30, clock)
        self.release = release

    def abort(self) -> int:
        self.release.wait()
        return super().abort()


class BlockingAbortConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.blocking_stream = BlockingAbortStream(release, clock)

    def newStream(self, flags: int = 0) -> BlockingAbortStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


class CompletedBlockingAbortStream(BlockingAbortStream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__(release, clock)
        self.chunks = []


class CompletedBlockingAbortConn(Conn):
    def __init__(
        self,
        release: threading.Event,
        clock: Clock,
        *,
        existing: Domain | None = None,
    ) -> None:
        super().__init__([], clock, existing=existing)
        self.blocking_stream = CompletedBlockingAbortStream(release, clock)

    def newStream(self, flags: int = 0) -> CompletedBlockingAbortStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


class InspectionDomain:
    def __init__(self, xml: str, active: bool) -> None:
        self.xml = xml
        self.active = active

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return 0


class InspectionConn:
    def __init__(self, domains: list[InspectionDomain]) -> None:
        self.domains = domains

    def listAllDomains(self, flags: int = 0) -> list[InspectionDomain]:  # noqa: N802
        return self.domains

    def storagePoolLookupByName(self, name: str) -> InspectionPool:  # noqa: N802
        assert name == "pool"
        return InspectionPool()


class InspectionVolume:
    def __init__(self, name: str) -> None:
        self.name = name

    def path(self) -> str:
        return f"/pool/{self.name}"


class InspectionPool:
    def storageVolLookupByName(self, name: str) -> InspectionVolume:  # noqa: N802
        return InspectionVolume(name)


class InspectionIdentity:
    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        names = {"root": 1, "source": 2, "scratch": 3, "appliance-x86_64.qcow2": 4}
        return RemoteDeviceIdentity("inode", 1, names[path.rsplit("/", 1)[-1]])
