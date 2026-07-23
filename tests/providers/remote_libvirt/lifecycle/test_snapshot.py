"""RemoteLibvirtSnapshotter tests — injected TLS opener + fake conn, no live host (ADR-0428).

Mirrors the LocalLibvirtSnapshotter unit suite (#1254): the snapshot mechanics are identical, only
the connection lifecycle differs (mutual-TLS ``remote_connection`` vs bare ``libvirt.open``). Unit
tests inject a fake connection/domain and a recording secret backend; the real ``libvirt.open``
adapter is ``live_vm``-only, exercised by the remote live proof.
"""

from __future__ import annotations

from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.snapshot import RemoteLibvirtSnapshotter
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error

_DOMAIN = "kdive-sys-remote"


class _FakeSnapshot:
    def __init__(self, name: str) -> None:
        self._name = name
        self.deleted = False

    def getName(self) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return self._name

    def delete(self, flags: int) -> int:
        self.deleted = True
        return 0


class _FakeSnapshotDomain:
    def __init__(self) -> None:
        self.snapshots: dict[str, _FakeSnapshot] = {}
        self.created: list[tuple[str, int]] = []
        self.reverted: list[tuple[str, int]] = []

    def snapshotCreateXML(self, xml: str, flags: int) -> _FakeSnapshot:  # noqa: N802
        name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
        self.created.append((name, flags))
        snap = _FakeSnapshot(name)
        self.snapshots[name] = snap
        return snap

    def snapshotLookupByName(self, name: str, flags: int) -> _FakeSnapshot:  # noqa: N802
        if name not in self.snapshots:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT)
        return self.snapshots[name]

    def revertToSnapshot(self, snap: _FakeSnapshot, flags: int) -> int:  # noqa: N802
        self.reverted.append((snap.getName(), flags))
        return 0

    def listAllSnapshots(self, flags: int) -> list[_FakeSnapshot]:  # noqa: N802
        return list(self.snapshots.values())


class _FakeSnapshotConn:
    def __init__(self, domain: _FakeSnapshotDomain | None) -> None:
        self._domain = domain
        self.closed = False

    def lookupByName(self, name: str) -> _FakeSnapshotDomain:  # noqa: N802
        if self._domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self._domain

    def close(self) -> None:
        self.closed = True


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
    )


def _snapshotter(
    domain: _FakeSnapshotDomain | None, tmp_path: Path
) -> tuple[RemoteLibvirtSnapshotter, _FakeSnapshotConn]:
    conn = _FakeSnapshotConn(domain)
    snapshotter = RemoteLibvirtSnapshotter(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )
    return snapshotter, conn


def test_create_memory_uses_no_disk_only_flag(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    snap, conn = _snapshotter(domain, tmp_path)
    snap.create(_DOMAIN, "before-bug", include_memory=True)
    assert domain.created == [("before-bug", 0)]  # full system checkpoint, no DISK_ONLY flag
    assert conn.closed is True  # remote_connection closes the TLS connection after the op


def test_create_disk_only_passes_disk_only_flag(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    snap, _ = _snapshotter(domain, tmp_path)
    snap.create(_DOMAIN, "fsonly", include_memory=False)
    assert domain.created == [("fsonly", libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY)]


def test_create_predeletes_existing_same_name_snapshot(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    stale = _FakeSnapshot("before-bug")
    domain.snapshots["before-bug"] = stale
    snap, _ = _snapshotter(domain, tmp_path)
    snap.create(_DOMAIN, "before-bug", include_memory=True)
    assert stale.deleted is True  # recycled name is clean before the fresh create


def test_revert_running_and_paused_flags(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    domain.snapshots["cp"] = _FakeSnapshot("cp")
    snap, _ = _snapshotter(domain, tmp_path)
    snap.revert(_DOMAIN, "cp", start_paused=False)
    snap.revert(_DOMAIN, "cp", start_paused=True)
    assert domain.reverted == [
        ("cp", libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING),
        ("cp", libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED),
    ]


def test_revert_missing_snapshot_is_configuration_error(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    snap, _ = _snapshotter(domain, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        snap.revert(_DOMAIN, "gone", start_paused=False)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_delete_is_idempotent_when_absent(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    snap, _ = _snapshotter(domain, tmp_path)
    snap.delete(_DOMAIN, "never-made")  # no snapshot, no error


def test_delete_all_removes_every_snapshot(tmp_path: Path) -> None:
    domain = _FakeSnapshotDomain()
    a, b = _FakeSnapshot("a"), _FakeSnapshot("b")
    domain.snapshots = {"a": a, "b": b}
    snap, _ = _snapshotter(domain, tmp_path)
    snap.delete_all(_DOMAIN)
    assert a.deleted and b.deleted


def test_delete_all_on_absent_domain_is_noop(tmp_path: Path) -> None:
    snap, _ = _snapshotter(None, tmp_path)  # lookupByName raises VIR_ERR_NO_DOMAIN
    snap.delete_all(_DOMAIN)  # idempotent: absent domain is success


def test_create_on_absent_domain_is_infrastructure_failure(tmp_path: Path) -> None:
    snap, _ = _snapshotter(None, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        snap.create(_DOMAIN, "cp", include_memory=True)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_create_libvirt_fault_is_infrastructure_failure(tmp_path: Path) -> None:
    class _Raising(_FakeSnapshotDomain):
        def snapshotCreateXML(self, xml: str, flags: int) -> _FakeSnapshot:  # noqa: N802
            raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    snap, _ = _snapshotter(_Raising(), tmp_path)
    with pytest.raises(CategorizedError) as exc:
        snap.create(_DOMAIN, "cp", include_memory=True)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"domain": _DOMAIN}
