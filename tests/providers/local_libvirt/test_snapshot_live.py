"""Live proof of ``LocalLibvirtSnapshotter`` against a real KVM domain (#1254, ADR-0378).

``live_vm``-gated (throwaway family, #1290): the operator points ``KDIVE_LIVE_VM_ROOTFS`` at a
bootable qcow2 (any kdive-ready rootfs works — the guest OS need not finish booting, only the QEMU
process must be running for a memory snapshot). The shared ``boot_throwaway_domain`` harness
(``kdive.testing.live_vm``) owns the overlay creation, the ``qemu:///system`` connect, and the
guaranteed destroy/undefine teardown (with the snapshot-metadata flag, so leftover snapshots do not
block undefine). Skips cleanly without the env or libvirt. Exercises the real ``virDomainSnapshot*``
path the worker handlers drive: memory create → running revert → paused revert (lands
``VIR_DOMAIN_PAUSED``, the ``systems.restore(start_paused=True)`` state) → delete → delete_all. The
MCP admission and ledger transitions are unit-tested separately; this proves the provider mechanics
against a real hypervisor.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from kdive.testing.live_vm import boot_throwaway_domain
from tests.live_vm import require_live_vm_throwaway


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_snapshotter_create_revert_resume_delete() -> None:  # pragma: no cover - live_vm
    contract = require_live_vm_throwaway("qemu:///system")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    from kdive.providers.local_libvirt.lifecycle.snapshot import (  # noqa: PLC0415
        LocalLibvirtSnapshotter,
    )

    name = f"kdive-snap-live-{uuid.uuid4().hex[:12]}"
    snapshotter = LocalLibvirtSnapshotter(connect=lambda: libvirt.open(contract.libvirt_uri))
    with boot_throwaway_domain(
        contract.rootfs, arch="x86_64", name=name, mode=contract.libvirt_uri, settle_s=2.0
    ) as live:
        dom: Any = live.domain  # the live libvirt virDomain (C-extension, no stubs)

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
