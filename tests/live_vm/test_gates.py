"""Unit tests for the live_vm env-contract resolvers + skip/fail gates (tests.live_vm)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_vm import (
    LiveVmEnvState,
    require_live_vm_bzimage,
    require_live_vm_provisioned,
    require_live_vm_remote,
    require_live_vm_throwaway,
    resolve_bzimage_contract,
    resolve_provisioned_contract,
    resolve_remote_contract,
    resolve_throwaway_contract,
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
