"""Tests for the customization-boot domain XML renderer (ADR-0345)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle import xml as xmlmod
from kdive.providers.local_libvirt.lifecycle.xml import render_customization_domain_xml
from kdive.providers.shared.libvirt_xml import QEMU_NS


def test_ssh_netdev_id_is_shared_constant() -> None:
    assert xmlmod.SYSTEM_SSH_NETDEV_ID == "kdivessh"


BID = UUID("11111111-2222-3333-4444-555555555555")


def test_customization_domain_pseries_tcg() -> None:
    xml = render_customization_domain_xml(
        BID,
        arch="ppc64le",
        disk_path="/d.qcow2",
        kernel_path=Path("/k/vmlinuz"),
        initrd_path=Path("/k/initrd"),
        accel="tcg",
        emulator="/usr/bin/qemu-system-ppc64",
    )
    root = ET.fromstring(xml)
    assert root.get("type") == "qemu"
    assert root.findtext("name") == f"kdive-build-{BID}"
    assert root.findtext("uuid") == str(BID)
    assert root.findtext("on_reboot") == "destroy"
    assert root.find("cpu") is None  # TCG: no <cpu>
    assert root.findtext("devices/emulator") == "/usr/bin/qemu-system-ppc64"
    channel = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    assert channel is not None
    assert channel.get("type") == "virtio"
    assert "root=/dev/vda console=hvc0 rw" in (root.findtext("os/cmdline") or "")
    # egress NIC present with restrict=off (namespaced qemu:arg)
    assert any("restrict=off" in (a.get("value") or "") for a in root.iter())


def test_customization_domain_x86_kvm_has_no_emulator_and_egress_on() -> None:
    xml = render_customization_domain_xml(
        BID,
        arch="x86_64",
        disk_path="/d.qcow2",
        kernel_path=Path("/k/vmlinuz"),
        initrd_path=Path("/k/initrd"),
        accel="kvm",
        emulator=None,
    )
    root = ET.fromstring(xml)
    assert root.get("type") == "kvm"
    assert root.find("devices/emulator") is None
    channel = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    assert channel is not None
    assert channel.get("type") == "virtio"
    vals = [a.get("value") or "" for a in root.iter()]
    assert any("restrict=off" in v for v in vals)
    assert any("virtio-net-pci" in v and "addr=0x10" in v for v in vals)  # q35 slot pin


def test_customization_restores_replaced_qemu_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ET, "_namespace_map", vars(ET)["_namespace_map"].copy())
    for replacement in ("foreign", "another"):
        ET.register_namespace(replacement, QEMU_NS)
        xml = render_customization_domain_xml(
            BID,
            arch="x86_64",
            disk_path="/d.qcow2",
            kernel_path=Path("/k/vmlinuz"),
            initrd_path=None,
            accel="kvm",
            emulator=None,
        )
        assert "<qemu:commandline>" in xml
        root = ET.fromstring(xml)
        args = [
            arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
        ]
        assert "-netdev" in args
        assert "user,id=kdivebuild,restrict=off" in args
        assert root.findtext("name") == f"kdive-build-{BID}"
        assert root.findtext("os/kernel") == "/k/vmlinuz"
