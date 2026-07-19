"""Unit tests for the pytest-free live_vm harness mechanism (kdive.testing.live_vm)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError
from kdive.testing.live_vm import (
    LiveVmEnvState,
    resolve_provisioned_contract,
    resolve_throwaway_contract,
    throwaway_domain_xml,
)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)  # noqa: S314 - kdive-rendered, trusted


def test_builder_x86_emits_q35_ttys0_hostpassthrough_acpi() -> None:
    root = _root(throwaway_domain_xml(name="kdive-x", arch="x86_64", disk_path="/d.qcow2"))
    assert root.get("type") == "kvm"
    os_type = root.find("./os/type")
    assert os_type is not None and os_type.get("machine") == "q35"
    cpu = root.find("./cpu")
    assert cpu is not None and cpu.get("mode") == "host-passthrough"
    assert root.find("./features/acpi") is not None
    assert root.find("./features/vmcoreinfo") is not None
    assert root.find("./devices/serial") is not None
    assert root.find("./devices/console") is not None


def test_builder_ppc64le_emits_pseries_hostmodel_no_acpi() -> None:
    root = _root(throwaway_domain_xml(name="kdive-p", arch="ppc64le", disk_path="/d.qcow2"))
    os_type = root.find("./os/type")
    assert os_type is not None and os_type.get("machine") == "pseries"
    cpu = root.find("./cpu")
    assert cpu is not None and cpu.get("mode") == "host-model"
    assert root.find("./features") is None
    assert root.find("./devices/serial") is not None


def test_builder_serial_log_sink_only_when_console_log_set(tmp_path: Path) -> None:
    without = _root(throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2"))
    assert without.find("./devices/serial/log") is None
    console = tmp_path / "c.log"
    with_log = _root(
        throwaway_domain_xml(name="b", arch="x86_64", disk_path="/d.qcow2", console_log=console)
    )
    log_el = with_log.find("./devices/serial/log")
    assert log_el is not None and log_el.get("file") == str(console)


def test_builder_ssh_netdev_present_iff_port_set() -> None:
    without = throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2")
    assert "hostfwd" not in without
    with_fwd = throwaway_domain_xml(
        name="b", arch="x86_64", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in with_fwd
    assert "addr=0x10" in with_fwd  # q35 pins the slot
    ppc = throwaway_domain_xml(
        name="c", arch="ppc64le", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "addr=0x10" not in ppc  # pseries does not


def test_builder_direct_kernel_and_default_console_cmdline(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"k")
    root = _root(
        throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2", kernel_path=kernel)
    )
    kernel_el = root.find("./os/kernel")
    cmdline_el = root.find("./os/cmdline")
    assert kernel_el is not None and kernel_el.text == str(kernel)
    assert cmdline_el is not None and cmdline_el.text == "root=/dev/vda console=ttyS0 rw"


def test_builder_unknown_arch_raises_configuration_error() -> None:
    with pytest.raises(CategorizedError):
        throwaway_domain_xml(name="a", arch="riscv64", disk_path="/d.qcow2")


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
