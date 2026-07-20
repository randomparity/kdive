"""Live proof for #747 (ADR-0233): real libvirt accepts kdive's gdbstub+preserve domain XML,
and the gdbstub is reachable (kdive's own ``rsp_reachable``) on a preserved early-boot panic.

`live_vm`-gated. The operator points ``KDIVE_LIVE_VM_BZIMAGE`` at a kernel image that panics
early in boot when it cannot mount its root (a bare bzImage with no usable rootfs), optionally
overriding ``KDIVE_LIBVIRT_URI`` (default ``qemu:///session`` so it needs no root). The test
renders the real provisioning XML, adds the direct-kernel ``<os>`` the install step adds in the
full pipeline, starts the domain against a deliberately empty disk to force the panic, and
asserts the stub answers ``rsp_reachable``. It tears down the transient domain and the scratch
disk in a ``finally``.
"""

from __future__ import annotations

import contextlib
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.shared.debug_common.rsp import rsp_reachable
from kdive.testing.live_vm import wait_for_panic

_GDB_PORT = 51234
# The SSH forward is rendered on every domain now (ADR-0281); this panic-boot test never reaches
# sshd, but render requires the port. Pinned distinct from the gdbstub port.
_SSH_PORT = 51235


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_preserve_crash_stub_is_reachable(tmp_path: Path) -> None:  # pragma: no cover
    bzimage = os.environ.get("KDIVE_LIVE_VM_BZIMAGE")
    if not bzimage or not Path(bzimage).is_file():
        pytest.skip("KDIVE_LIVE_VM_BZIMAGE (an early-panicking kernel image) unavailable")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    uri = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///session")
    garbage_disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    _make_empty_qcow2(garbage_disk)
    console.write_text("")

    profile = ProvisioningProfile.parse(_profile_data(garbage_disk))
    base_xml = render_domain_xml(
        uuid4(),
        profile,
        disk_path=str(garbage_disk),
        gdb_port=_GDB_PORT,
        ssh_port=_SSH_PORT,
        kernel_path=Path(bzimage),
    )
    final_xml = _with_direct_kernel(base_xml, bzimage=bzimage, console=console)

    conn = libvirt.open(uri)
    dom = None
    try:
        # createXML raising here would itself be a failure: it proves libvirt accepts the new
        # pvpanic + <on_crash>preserve</on_crash> + -gdb passthrough XML.
        dom = conn.createXML(final_xml, 0)
        assert wait_for_panic(console, 30.0), "no early-boot kernel panic on console"
        # The crash signal is the console panic; the stub stays reachable on the halted vCPU
        # (domain may remain RUNNING with panic=0, so this does NOT assert VIR_DOMAIN_CRASHED).
        assert rsp_reachable("127.0.0.1", _GDB_PORT), "gdbstub not reachable on the halted panic"
    finally:
        if dom is not None:
            with contextlib.suppress(libvirt.libvirtError):
                dom.destroy()
        conn.close()


def _profile_data(disk: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 1024,
        "disk_gb": 5,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {
            "local-libvirt": {
                "domain_xml_params": {"machine": "pc-q35-9.0"},
                "rootfs": {"kind": "local", "path": str(disk)},
                "debug": {"gdbstub": True, "preserve_on_crash": True},
            }
        },
    }


def _with_direct_kernel(base_xml: str, *, bzimage: str, console: Path) -> str:
    """Add the direct-kernel <os> (install.py's job) + a writable serial log to the base XML."""
    root = ET.fromstring(base_xml)  # noqa: S314 - kdive-rendered, trusted
    os_el = root.find("os")
    assert os_el is not None
    ET.SubElement(os_el, "kernel").text = bzimage
    # No usable rootfs in the empty disk -> VFS panic; panic=0 halts (does not reboot).
    ET.SubElement(os_el, "cmdline").text = "console=ttyS0 panic=0 root=/dev/vda"
    serial_log = root.find("./devices/serial/log")
    assert serial_log is not None
    serial_log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")


def _make_empty_qcow2(path: Path) -> None:
    import subprocess  # noqa: PLC0415

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(path), "1G"],
        check=True,
        capture_output=True,
    )
