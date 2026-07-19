"""Unit tests for the live_vm skip/fail gates (tests.live_vm)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_vm import require_live_vm_provisioned, require_live_vm_throwaway


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
