from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    LocalExternalBootOperationLease,
    LocalExternalBootSessionFactory,
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

    def pin(self, lease: LocalExternalBootOperationLease) -> FakePin:
        if not isinstance(lease, FakeLease):
            raise TypeError("foreign operation lease")
        if lease.released:
            raise RuntimeError("operation lease is released")
        return FakePin(lease)


LANE = FakeLane()


def _lease() -> FakeLease:
    return FakeLease()


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
        stat_overlay=lambda _path: (8, 9),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )


def test_factory_pins_before_open_and_lease_cannot_release_while_session_live() -> None:
    events: list[str] = []
    lease = _lease()
    session = _factory(events).open(lease)
    assert events[:2] == ["connection.open", f"domain.open:kdive-{SYSTEM_ID}"]
    with pytest.raises(RuntimeError, match="pinned"):
        lease.release()
    session.close()
    lease.release()


@pytest.mark.parametrize("lease", [None, object()])
def test_missing_or_foreign_lease_opens_nothing(lease: object | None) -> None:
    events: list[str] = []
    with pytest.raises((TypeError, ValueError)):
        _factory(events).open(lease)  # ty: ignore[invalid-argument-type]
    assert events == []


def test_released_lease_opens_nothing() -> None:
    events: list[str] = []
    lease = _lease()
    lease.release()
    events.clear()
    with pytest.raises(RuntimeError, match="released"):
        _factory(events).open(lease)
    assert events == []


def test_inspection_is_exact_immutable_and_validates_ownership() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease())
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
        _factory(events, foreign).open(_lease())


def test_guest_fences_and_rechecks_overlay_and_can_reopen() -> None:
    events: list[str] = []
    session = _factory(events).open(_lease())
    with session.guest() as guest:
        assert guest.exists("/etc/os-release") == 1
    with session.guest() as guest:
        assert guest.exists("/etc/os-release") == 1
    assert events.count("guest.open") == 2
    assert events.index("domain.active") < events.index(f"guest.drive:{OVERLAY}:qcow2")
    session.close()


def test_close_poisons_wrappers_and_releases_pin_last() -> None:
    events: list[str] = []
    lease = _lease()
    session = _factory(events).open(lease)
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
    factory = LocalExternalBootSessionFactory(
        pin_lease=LANE.pin,
        connect=lambda: FaultingConn(events, domain),
        open_artifact_root=lambda _lease: 41,
        open_guest=lambda: Guest(events),
        stat_overlay=lambda _path: (8, 9),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease)
    with pytest.raises(ExceptionGroup) as raised:
        session.close()
    assert len(raised.value.exceptions) == 2
    lease.release()


def test_active_domain_blocks_guest_before_open() -> None:
    events: list[str] = []
    domain = Domain(events)
    domain.active = True
    session = _factory(events, domain).open(_lease())
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
        stat_overlay=lambda _path: next(stats),
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease())
    with pytest.raises(ValueError, match="overlay changed"), session.guest():
        pass
    assert "guest.open" not in events
    session.close()


def test_partial_construction_closes_every_acquired_resource_and_pin_last() -> None:
    events: list[str] = []
    domain = Domain(events, _xml(overlay="/wrong.qcow2"))
    lease = _lease()
    with pytest.raises(ValueError, match="overlay"):
        _factory(events, domain).open(lease)
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
        stat_overlay=lambda _path: (8, 9),
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
    session = factory.open(_lease())
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
        stat_overlay=lambda _path: (8, 9),
        close_descriptor=lambda _fd: None,
    )
    session = factory.open(_lease())
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
        stat_overlay=lambda _path: (8, 9),
        close_descriptor=fault_descriptor,
    )
    session = factory.open(lease)
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
        stat_overlay=lambda _path: (8, 9),
        close_descriptor=lambda _fd: events.append("artifact.close"),
    )
    session = factory.open(lease)
    with pytest.raises(LookupError, match=f"{fault_at} primary"), session.guest():
        pass
    if fault_at != "factory":
        assert events[-2:] == ["guest.shutdown", "guest.close"]
    with pytest.raises(RuntimeError, match="pinned"):
        lease.release()
    session.close()
    lease.release()
