from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ExpectedOperationOwnership,
    LocalExternalBootOperationLease,
    LocalExternalBootSessionFactory,
    OpenArtifactRoot,
    OperationOwnership,
    PinnedOperationOwnership,
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

    def find(self, path: str) -> list[str]:
        return [path]

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


def _factory(events: list[str], domain: Domain | None = None) -> LocalExternalBootSessionFactory:
    selected = domain or Domain(events)
    return LocalExternalBootSessionFactory(
        connect=lambda: events.append("connection.open") or Conn(events, selected),
        pin_lease=LANE.pin,
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
