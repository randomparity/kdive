"""Live proof of ``LocalLibvirtTrafficCapture`` against a real KVM domain (#1258, ADR-0384).

``live_vm``-gated: the operator points ``KDIVE_LIVE_VM_ROOTFS`` at a bootable qcow2 (any
kdive-ready rootfs works — the guest need not finish booting, only the QEMU process must run so
its SLIRP netdev exists). Skips cleanly without libvirt or the image. Exercises the real QEMU
``filter-dump`` QMP path the worker handler drives: attach a filter-dump on the ``kdivessh``
SSH-forward netdev, generate packets by connecting to the host-forwarded port (SLIRP injects the
forwarded connection onto that netdev), detach, and assert the on-disk pcap is a valid libpcap
file with at least one captured record. The MCP admission, egress, and store transitions are
unit-tested separately; this proves the provider mechanic against a real hypervisor.

Defaults to ``qemu:///session`` so the QEMU process runs as the invoking user and the pcap is
readable without the ADR-0223 root-readback wall (that wall is unit-tested via ``read_pcap_bytes``).
"""

from __future__ import annotations

import contextlib
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from kdive.providers.local_libvirt.lifecycle.xml import SYSTEM_SSH_NETDEV_ID


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.live_vm
def test_live_vm_traffic_capture_filter_dump() -> None:  # pragma: no cover - live_vm
    rootfs = os.environ.get("KDIVE_LIVE_VM_ROOTFS")
    if not rootfs or not Path(rootfs).is_file():
        pytest.skip("KDIVE_LIVE_VM_ROOTFS (a bootable rootfs qcow2) unavailable")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
        import libvirt_qemu  # noqa: PLC0415  # operator-provided (QEMU-specific binding)
    except ImportError:
        pytest.skip("libvirt-python / libvirt_qemu unavailable")

    from kdive.artifacts.pcap_count import count_pcap_packets  # noqa: PLC0415
    from kdive.providers.local_libvirt.lifecycle.traffic_capture import (  # noqa: PLC0415
        LocalLibvirtTrafficCapture,
    )

    uri = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///session")
    if uri == "qemu:///session":
        # Session-mode libvirt derives its per-domain QMP socket under $XDG_CONFIG_HOME; the test
        # harness points that at a deep pytest tmp path that overflows the 108-byte UNIX socket
        # limit. Redirect it to a short path before connecting (system mode is unaffected).
        short_xdg = Path(f"/tmp/kdive-cl-{uuid.uuid4().hex[:8]}")  # noqa: S108
        short_xdg.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_CONFIG_HOME"] = str(short_xdg)
    name = f"kdive-cap-live-{uuid.uuid4().hex[:12]}"
    port = _free_port()
    disk = Path(rootfs).with_name(f"{name}.qcow2")
    pcap_dir = Path(tempfile.mkdtemp(prefix="kdive-pcap-live-"))
    pcap_file = pcap_dir / f"{name}.pcap"
    qom_id = f"kdive-dump-{name}"

    import subprocess  # noqa: PLC0415

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", rootfs, str(disk)],
        check=True,
        capture_output=True,
    )
    # The SSH-forward netdev (SYSTEM_SSH_NETDEV_ID) carries a host->guest:22 forward, exactly like
    # real provisioning. Connecting to the host side injects packets onto this netdev for capture.
    domain_xml = f"""
    <domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
      <name>{name}</name>
      <memory unit='MiB'>1024</memory>
      <vcpu>1</vcpu>
      <os><type arch='x86_64' machine='q35'>hvm</type></os>
      <devices>
        <disk type='file' device='disk'>
          <driver name='qemu' type='qcow2'/>
          <source file='{disk}'/>
          <target dev='vda' bus='virtio'/>
        </disk>
        <serial type='pty'><target port='0'/></serial>
      </devices>
      <qemu:commandline>
        <qemu:arg value='-netdev'/>
        <qemu:arg value='user,id={SYSTEM_SSH_NETDEV_ID},hostfwd=tcp:127.0.0.1:{port}-:22'/>
        <qemu:arg value='-device'/>
        <qemu:arg value='virtio-net-pci,netdev={SYSTEM_SSH_NETDEV_ID},addr=0x10'/>
      </qemu:commandline>
    </domain>
    """
    capturer = LocalLibvirtTrafficCapture(
        connect=lambda: libvirt.open(uri), monitor=libvirt_qemu.qemuMonitorCommand
    )
    conn = libvirt.open(uri)
    dom = None
    try:
        dom = conn.defineXML(domain_xml)
        dom.create()  # a running QEMU is required for the SLIRP netdev to exist
        time.sleep(2)
        assert dom.isActive()

        capturer.attach(name, qom_id=qom_id, dest_path=str(pcap_file), snaplen=128)
        # Generate traffic on the SSH-forward netdev: each connect makes SLIRP inject packets
        # toward the guest, which the filter-dump captures regardless of a guest-side listener.
        for _ in range(8):
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", port), timeout=1) as sock,
            ):
                sock.sendall(b"kdive-capture-probe\n")
            time.sleep(0.2)
        capturer.detach(name, qom_id=qom_id)

        data = pcap_file.read_bytes()
        assert count_pcap_packets(data) > 0, "filter-dump captured no packets"
    finally:
        if dom is not None:
            with contextlib.suppress(libvirt.libvirtError):
                if dom.isActive():
                    dom.destroy()
            with contextlib.suppress(libvirt.libvirtError):
                dom.undefine()
        conn.close()
        disk.unlink(missing_ok=True)
        pcap_file.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            pcap_dir.rmdir()
