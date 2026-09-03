"""Unit tests for the live_vm env-contract resolvers + skip/fail gates (tests.live_vm)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import libvirt
import pytest

from tests.live_vm import (
    _STORAGE_DOUBLE_PROBE,
    LiveVmEnvState,
    _is_local_session_uri,
    require_live_vm_bzimage,
    require_live_vm_provisioned,
    require_live_vm_remote,
    require_live_vm_storage_double,
    require_live_vm_throwaway,
    require_live_vm_vmlinux,
    resolve_bzimage_contract,
    resolve_provisioned_contract,
    resolve_remote_contract,
    resolve_storage_double_contract,
    resolve_throwaway_contract,
    resolve_vmlinux_contract,
)

_REMOTE_URI = "qemu+tls://host.example/system"


def _set_remote_companions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every remote companion env (base image, S3 endpoint+bucket, reconciler) to valid data."""
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_BASE_IMAGE", "kdive-base-fedora.qcow2")
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://s3.example:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive-artifacts")
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_RECONCILER", "http://127.0.0.1:9466/metrics")


def test_throwaway_absent_when_rootfs_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_ROOTFS", raising=False)
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.ABSENT
    assert "KDIVE_LIVE_VM_ROOTFS" in result.reason


def test_throwaway_misconfigured_when_rootfs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", "/nonexistent/rootfs.qcow2")
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.MISCONFIGURED


def test_throwaway_misconfigured_when_parent_dir_not_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    rootfs = ro_dir / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    ro_dir.chmod(0o500)  # readable+executable, not writable
    try:
        monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
        result = resolve_throwaway_contract("qemu:///system")
        assert result.state is LiveVmEnvState.MISCONFIGURED
        assert "writable" in result.reason
    finally:
        ro_dir.chmod(0o700)


def test_throwaway_available_resolves_default_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///system"
    assert result.contract.rootfs == rootfs


def test_throwaway_available_honors_libvirt_uri_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///session")
    result = resolve_throwaway_contract("qemu:///system")
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///session"


def test_bzimage_absent_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_BZIMAGE", raising=False)
    result = resolve_bzimage_contract("qemu:///session")
    assert result.state is LiveVmEnvState.ABSENT
    assert "KDIVE_LIVE_VM_BZIMAGE" in result.reason


def test_bzimage_misconfigured_when_not_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_BZIMAGE", "/nonexistent/bzImage")
    result = resolve_bzimage_contract("qemu:///session")
    assert result.state is LiveVmEnvState.MISCONFIGURED


def test_bzimage_available_resolves_default_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bzimage = tmp_path / "bzImage"
    bzimage.write_bytes(b"kernel")
    monkeypatch.setenv("KDIVE_LIVE_VM_BZIMAGE", str(bzimage))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    result = resolve_bzimage_contract("qemu:///session")
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.bzimage == bzimage
    assert result.contract.libvirt_uri == "qemu:///session"


def test_bzimage_available_honors_libvirt_uri_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bzimage = tmp_path / "bzImage"
    bzimage.write_bytes(b"kernel")
    monkeypatch.setenv("KDIVE_LIVE_VM_BZIMAGE", str(bzimage))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    result = resolve_bzimage_contract("qemu:///session")
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///system"


def test_bzimage_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_BZIMAGE", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_bzimage()


def test_bzimage_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_BZIMAGE", "/nonexistent/bzImage")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_bzimage()


def test_bzimage_returns_contract_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bzimage = tmp_path / "bzImage"
    bzimage.write_bytes(b"kernel")
    monkeypatch.setenv("KDIVE_LIVE_VM_BZIMAGE", str(bzimage))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    contract = require_live_vm_bzimage()
    assert contract.libvirt_uri == "qemu:///session"
    assert contract.bzimage == bzimage


def test_vmlinux_absent_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_VMLINUX", raising=False)
    result = resolve_vmlinux_contract()
    assert result.state is LiveVmEnvState.ABSENT
    assert "KDIVE_LIVE_VM_VMLINUX" in result.reason


def test_vmlinux_misconfigured_when_not_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_VMLINUX", "/nonexistent/vmlinux")
    result = resolve_vmlinux_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED


def test_vmlinux_returns_matching_debug_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vmlinux = tmp_path / "vmlinux.debug"
    vmlinux.write_bytes(b"ELF")
    monkeypatch.setenv("KDIVE_LIVE_VM_VMLINUX", str(vmlinux))
    contract = require_live_vm_vmlinux()
    assert contract.vmlinux == vmlinux


def test_vmlinux_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_VMLINUX", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_vmlinux()


def test_vmlinux_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_VMLINUX", "/nonexistent/vmlinux")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_vmlinux()


def test_provisioned_absent_when_system_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_SYSTEM_ID", raising=False)
    result = resolve_provisioned_contract("qemu:///system")
    assert result.state is LiveVmEnvState.ABSENT


def test_provisioned_misconfigured_on_partial_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_SYSTEM_ID", "sys-123")
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)  # a real required var, left unset
    result = resolve_provisioned_contract("qemu:///system")
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_S3_BUCKET" in result.reason


def test_throwaway_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_ROOTFS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_throwaway()


def test_throwaway_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", "/nonexistent/rootfs.qcow2")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_throwaway()


def test_throwaway_returns_contract_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    contract = require_live_vm_throwaway("qemu:///system")
    assert contract.libvirt_uri == "qemu:///system"


def test_throwaway_session_required_fails_when_override_moves_off_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_throwaway("qemu:///session", session_required=True)


def test_throwaway_session_required_passes_on_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    contract = require_live_vm_throwaway("qemu:///session", session_required=True)
    assert contract.libvirt_uri.startswith("qemu:///session")


@pytest.mark.parametrize(
    "uri",
    (
        "qemu:///session",
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
        "qemu+unix:///session?socket=/tmp/operator/virtqemud-sock",
    ),
)
def test_throwaway_session_required_accepts_local_session_uris(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uri: str
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", uri)

    assert require_live_vm_throwaway(session_required=True).libvirt_uri == uri


@pytest.mark.parametrize(
    "uri",
    (
        "qemu:///system",
        "qemu+ssh://host/session",
        "qemu+unix://host/session?socket=/run/libvirt.sock",
        "qemu+unix:///system?socket=/run/libvirt.sock",
        "qemu+unix:///session",
        "qemu+unix:///session?socket=relative.sock",
        "qemu+unix:///session?socket=/run/a&socket=/run/b",
        "qemu+unix:///session?socket=/run/libvirt.sock&auth=none",
        "qemu:///session-extra",
    ),
)
def test_throwaway_session_required_rejects_nonlocal_or_malformed_session_uris(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uri: str
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", uri)

    with pytest.raises(pytest.fail.Exception):
        require_live_vm_throwaway(session_required=True)


def test_provisioned_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_SYSTEM_ID", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_provisioned()


def test_remote_absent_when_uri_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_URI", raising=False)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.ABSENT
    assert "KDIVE_LIVE_VM_REMOTE_URI" in result.reason


def test_remote_misconfigured_when_uri_not_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", "qemu:///system")
    _set_remote_companions(monkeypatch)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "qemu+tls://" in result.reason


def test_remote_misconfigured_when_uri_carries_no_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", f"{_REMOTE_URI}?no_verify=1")
    _set_remote_companions(monkeypatch)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "no_verify" in result.reason


def test_remote_misconfigured_when_base_image_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)
    _set_remote_companions(monkeypatch)
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_BASE_IMAGE", raising=False)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_LIVE_VM_REMOTE_BASE_IMAGE" in result.reason


def test_remote_misconfigured_on_partial_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)
    _set_remote_companions(monkeypatch)
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_S3_BUCKET" in result.reason


def test_remote_misconfigured_when_reconciler_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)
    _set_remote_companions(monkeypatch)
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_RECONCILER", raising=False)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_LIVE_VM_REMOTE_RECONCILER" in result.reason


def test_remote_available_resolves_full_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)
    _set_remote_companions(monkeypatch)
    result = resolve_remote_contract()
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.libvirt_uri == _REMOTE_URI
    assert result.contract.base_image == "kdive-base-fedora.qcow2"
    assert result.contract.s3_endpoint_url == "http://s3.example:9000"
    assert result.contract.s3_bucket == "kdive-artifacts"
    assert result.contract.reconciler == "http://127.0.0.1:9466/metrics"


def test_remote_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_URI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_remote()


def test_remote_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)  # set, but no companions
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_BASE_IMAGE", raising=False)
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)
    monkeypatch.delenv("KDIVE_LIVE_VM_REMOTE_RECONCILER", raising=False)
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_remote()


def test_remote_returns_contract_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", _REMOTE_URI)
    _set_remote_companions(monkeypatch)
    contract = require_live_vm_remote()
    assert contract.libvirt_uri == _REMOTE_URI
    assert contract.base_image == "kdive-base-fedora.qcow2"


# --- the storage-double fidelity gate (#2164) ---------------------------------------------

_PUBLISHED_SOCKET = "qemu+unix:///session?socket=/run/user/1000/libvirt/virtqemud-sock"


class _FakeConn:
    """A libvirt connection slice: listStoragePools + close, each recorded."""

    def __init__(self, *, list_error: libvirt.libvirtError | None = None) -> None:
        self._list_error = list_error
        self.listed = False
        self.closed = False

    def listStoragePools(self) -> list[str]:  # noqa: N802 - libvirt binding name
        self.listed = True
        if self._list_error is not None:
            raise self._list_error
        return []

    def close(self) -> None:
        self.closed = True


def _opener(conn: _FakeConn | None = None, *, error: bool = False, calls: list[str] | None = None):
    """Build a libvirt.open replacement that records its URIs."""

    def _open(uri: str) -> _FakeConn:
        if calls is not None:
            calls.append(uri)
        if error:
            raise libvirt.libvirtError("cannot connect")
        assert conn is not None
        return conn

    return _open


@pytest.fixture(autouse=True)
def _reset_storage_double_latch() -> Iterator[None]:
    """ADR-0580's test consequence: a fabricated verdict never leaks out of a test."""
    saved = dict(_STORAGE_DOUBLE_PROBE)
    _STORAGE_DOUBLE_PROBE.clear()
    try:
        yield
    finally:
        _STORAGE_DOUBLE_PROBE.clear()
        _STORAGE_DOUBLE_PROBE.update(saved)


def test_storage_double_absent_when_no_session_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(error=True))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.ABSENT
    assert "qemu:///session" in result.reason


def test_storage_double_absent_when_the_storage_driver_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    conn = _FakeConn(list_error=libvirt.libvirtError("no storage driver"))
    monkeypatch.setattr(libvirt, "open", _opener(conn))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.ABSENT
    assert "storage driver" in result.reason
    assert conn.closed


def test_storage_double_misconfigured_when_declared_uri_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu+unix:///session?socket=/nonexistent/sock")
    monkeypatch.setattr(libvirt, "open", _opener(error=True))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_LIBVIRT_URI" in result.reason


def test_storage_double_misconfigured_when_override_moves_off_a_local_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proof's pool target is a client-side tmp_path, so system mode means nothing here."""
    calls: list[str] = []
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setattr(libvirt, "open", _opener(error=True, calls=calls))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "local session" in result.reason
    assert calls == []


def test_storage_double_available_closes_its_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    conn = _FakeConn()
    monkeypatch.setattr(libvirt, "open", _opener(conn))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///session"
    assert conn.listed
    assert conn.closed


def test_storage_double_available_honors_libvirt_uri_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", _PUBLISHED_SOCKET)
    monkeypatch.setattr(libvirt, "open", _opener(_FakeConn()))
    result = resolve_storage_double_contract("qemu:///session")
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.libvirt_uri == _PUBLISHED_SOCKET


def test_storage_double_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(error=True))
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_storage_double()


def test_storage_double_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setattr(libvirt, "open", _opener(error=True))
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_storage_double()


def test_storage_double_probes_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0580: a gate probing a live resource probes once per process and reuses the verdict."""
    calls: list[str] = []
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(_FakeConn(), calls=calls))
    first = resolve_storage_double_contract("qemu:///session")
    second = resolve_storage_double_contract("qemu:///session")
    assert calls == ["qemu:///session"]
    assert first == second


def test_storage_double_latches_an_absent_verdict_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The available side latches as hard as the unavailable side (ADR-0580, both directions)."""
    calls: list[str] = []
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(error=True, calls=calls))
    first = resolve_storage_double_contract("qemu:///session")
    second = resolve_storage_double_contract("qemu:///session")
    assert calls == ["qemu:///session"]
    assert first.state is LiveVmEnvState.ABSENT
    assert second.state is LiveVmEnvState.ABSENT


def test_storage_double_latch_is_keyed_by_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(_FakeConn(), calls=calls))
    resolve_storage_double_contract("qemu:///session")
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", _PUBLISHED_SOCKET)
    second = resolve_storage_double_contract("qemu:///session")
    assert calls == ["qemu:///session", _PUBLISHED_SOCKET]
    assert second.contract is not None
    assert second.contract.libvirt_uri == _PUBLISHED_SOCKET


def test_storage_double_latched_probe_still_honours_the_env_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latch holds the probe outcome, not the resolution.

    ``_resolved_uri`` returns the same string for an unset variable defaulting to
    ``qemu:///session`` and for one explicitly set to it, so latching the whole resolution would
    serve the ABSENT verdict back under the set case — turning a mis-provisioned runner into a
    silent skip, which is the one outcome the module's discipline exists to prevent.
    """
    calls: list[str] = []
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    monkeypatch.setattr(libvirt, "open", _opener(error=True, calls=calls))
    assert resolve_storage_double_contract("qemu:///session").state is LiveVmEnvState.ABSENT
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///session")
    assert resolve_storage_double_contract("qemu:///session").state is LiveVmEnvState.MISCONFIGURED
    assert calls == ["qemu:///session"]


@pytest.mark.parametrize(
    "uri",
    [
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock",
        "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
    ],
)
def test_storage_double_accepts_the_published_socket_uri_shape(uri: str) -> None:
    """The two values live.yml exports as KDIVE_LIBVIRT_URI before running this tier."""
    assert _is_local_session_uri(uri)
