"""Live proof of ``LocalLibvirtSnapshotter`` against a real KVM domain (#1254, ADR-0378).

``live_vm``-gated: the operator points ``KDIVE_LIVE_VM_ROOTFS`` at a bootable qcow2 (any
kdive-ready rootfs works — the guest OS need not finish booting, only the QEMU process must be
running for a memory snapshot). Skips cleanly without libvirt or the image. Exercises the real
``virDomainSnapshot*`` path the worker handlers drive: memory create → running revert → paused
revert (lands ``VIR_DOMAIN_PAUSED``, the ``systems.restore(start_paused=True)`` state) → delete →
delete_all. The MCP admission and ledger transitions are unit-tested separately; this proves the
provider mechanics against a real hypervisor.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path

import pytest


@pytest.mark.live_vm
def test_live_vm_snapshotter_create_revert_resume_delete() -> None:  # pragma: no cover - live_vm
    rootfs = os.environ.get("KDIVE_LIVE_VM_ROOTFS")
    if not rootfs or not Path(rootfs).is_file():
        pytest.skip("KDIVE_LIVE_VM_ROOTFS (a bootable rootfs qcow2) unavailable")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    from kdive.providers.local_libvirt.lifecycle.snapshot import (  # noqa: PLC0415
        LocalLibvirtSnapshotter,
    )

    uri = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///system")
    name = f"kdive-snap-live-{uuid.uuid4().hex[:12]}"
    # The overlay lives beside the rootfs so it inherits that directory's libvirt access + SELinux
    # label (real provisioning stages overlays there); a throwaway backed by the read-only base.
    disk = Path(rootfs).with_name(f"{name}.qcow2")
    import subprocess  # noqa: PLC0415

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", rootfs, str(disk)],
        check=True,
        capture_output=True,
    )
    domain_xml = f"""
    <domain type='kvm'>
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
    </domain>
    """
    snapshotter = LocalLibvirtSnapshotter(connect=lambda: libvirt.open(uri))
    conn = libvirt.open(uri)
    dom = None
    try:
        dom = conn.defineXML(domain_xml)
        dom.create()  # a running QEMU is required for a memory snapshot
        time.sleep(2)
        assert dom.isActive()

        snapshotter.create(name, "cp1", include_memory=True)
        assert "cp1" in {snap.getName() for snap in dom.listAllSnapshots(0)}

        snapshotter.revert(name, "cp1", start_paused=False)
        assert dom.isActive()

        # start_paused lands the domain in VIR_DOMAIN_PAUSED — the systems.restore(start_paused)
        # state a gdbstub debug.start_session attaches to before control.power(resume).
        snapshotter.revert(name, "cp1", start_paused=True)
        assert dom.state()[0] == libvirt.VIR_DOMAIN_PAUSED

        snapshotter.delete(name, "cp1")
        assert "cp1" not in {snap.getName() for snap in dom.listAllSnapshots(0)}

        snapshotter.create(name, "disk-a", include_memory=False)
        snapshotter.create(name, "disk-b", include_memory=False)
        snapshotter.delete_all(name)
        assert dom.listAllSnapshots(0) == []
    finally:
        with contextlib.suppress(Exception):
            snapshotter.delete_all(name)
        if dom is not None:
            with contextlib.suppress(libvirt.libvirtError):
                if dom.isActive():
                    dom.destroy()
            with contextlib.suppress(libvirt.libvirtError):
                dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
        conn.close()
        disk.unlink(missing_ok=True)
