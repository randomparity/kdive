"""LocalLibvirtSnapshotter: internal snapshot create/revert/delete (#1254, ADR-0378).

Unit tests inject a fake libvirt connection/domain; the real ``libvirt.open`` adapter is
``live_vm``-only, exercised by the Task 14 live proof.
"""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.snapshot import LocalLibvirtSnapshotter


class _FakeSnapshot:
    def __init__(self, name: str) -> None:
        self._name = name
        self.deleted = False

    def getName(self) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return self._name

    def delete(self, flags: int) -> int:
        self.deleted = True
        return 0


class _FakeLibvirtError(libvirt.libvirtError):
    def __init__(self, code: int) -> None:
        self._code = code
        super().__init__("fake libvirt error")

    def get_error_code(self) -> int:
        return self._code


class _FakeDomain:
    def __init__(self) -> None:
        self.snapshots: dict[str, _FakeSnapshot] = {}
        self.created: list[tuple[str, int]] = []
        self.reverted: list[tuple[str, int]] = []

    def snapshotCreateXML(self, xml: str, flags: int) -> _FakeSnapshot:  # noqa: N802
        # Extract the <name> from the minimal snapshot XML the snapshotter builds.
        name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
        self.created.append((name, flags))
        snap = _FakeSnapshot(name)
        self.snapshots[name] = snap
        return snap

    def snapshotLookupByName(self, name: str, flags: int) -> _FakeSnapshot:  # noqa: N802
        if name not in self.snapshots:
            raise _FakeLibvirtError(libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT)
        return self.snapshots[name]

    def revertToSnapshot(self, snap: _FakeSnapshot, flags: int) -> int:  # noqa: N802
        self.reverted.append((snap.getName(), flags))
        return 0

    def listAllSnapshots(self, flags: int) -> list[_FakeSnapshot]:  # noqa: N802
        return list(self.snapshots.values())


class _FakeConn:
    def __init__(self, domain: _FakeDomain | None) -> None:
        self._domain = domain
        self.closed = False

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802
        if self._domain is None:
            raise _FakeLibvirtError(libvirt.VIR_ERR_NO_DOMAIN)
        return self._domain

    def close(self) -> int:
        self.closed = True
        return 0


def _snapshotter(domain: _FakeDomain | None) -> tuple[LocalLibvirtSnapshotter, _FakeConn]:
    conn = _FakeConn(domain)
    return LocalLibvirtSnapshotter(connect=lambda: conn), conn


def test_create_memory_uses_no_disk_only_flag() -> None:
    domain = _FakeDomain()
    snap, conn = _snapshotter(domain)
    snap.create("dom", "before-bug", include_memory=True)
    assert domain.created == [("before-bug", 0)]  # full system checkpoint, no DISK_ONLY flag
    assert conn.closed is True


def test_create_disk_only_passes_disk_only_flag() -> None:
    domain = _FakeDomain()
    snap, _ = _snapshotter(domain)
    snap.create("dom", "fsonly", include_memory=False)
    assert domain.created == [("fsonly", libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY)]


def test_create_predeletes_existing_same_name_snapshot() -> None:
    domain = _FakeDomain()
    stale = _FakeSnapshot("before-bug")
    domain.snapshots["before-bug"] = stale
    snap, _ = _snapshotter(domain)
    snap.create("dom", "before-bug", include_memory=True)
    # The stale snapshot is deleted before the fresh create, so a recycled name is clean.
    assert stale.deleted is True


def test_revert_running_and_paused_flags() -> None:
    domain = _FakeDomain()
    domain.snapshots["cp"] = _FakeSnapshot("cp")
    snap, _ = _snapshotter(domain)
    snap.revert("dom", "cp", start_paused=False)
    snap.revert("dom", "cp", start_paused=True)
    assert domain.reverted == [
        ("cp", libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING),
        ("cp", libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED),
    ]


def test_revert_missing_snapshot_is_configuration_error() -> None:
    domain = _FakeDomain()
    snap, _ = _snapshotter(domain)
    with pytest.raises(CategorizedError) as exc:
        snap.revert("dom", "gone", start_paused=False)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_delete_is_idempotent_when_absent() -> None:
    domain = _FakeDomain()
    snap, _ = _snapshotter(domain)
    snap.delete("dom", "never-made")  # no snapshot, no error


def test_delete_all_removes_every_snapshot() -> None:
    domain = _FakeDomain()
    a, b = _FakeSnapshot("a"), _FakeSnapshot("b")
    domain.snapshots = {"a": a, "b": b}
    snap, _ = _snapshotter(domain)
    snap.delete_all("dom")
    assert a.deleted and b.deleted


def test_delete_all_on_absent_domain_is_noop() -> None:
    snap, _ = _snapshotter(None)  # lookupByName raises VIR_ERR_NO_DOMAIN
    snap.delete_all("dom")  # idempotent: absent domain is success


def test_create_on_absent_domain_is_infrastructure_failure() -> None:
    snap, _ = _snapshotter(None)
    with pytest.raises(CategorizedError) as exc:
        snap.create("dom", "cp", include_memory=True)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
