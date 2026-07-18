"""Tests for the customization-boot domain XML renderer (ADR-0345)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle import xml as xmlmod
from kdive.providers.local_libvirt.lifecycle.xml import render_customization_domain_xml


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
    vals = [a.get("value") or "" for a in root.iter()]
    assert any("restrict=off" in v for v in vals)
    assert any("virtio-net-pci" in v and "addr=0x10" in v for v in vals)  # q35 slot pin
