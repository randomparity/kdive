"""Shared domain rendering for live debug tests."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from tests.mcp.debug.session_support import PROFILE
from tests.support.domain_ownership import drop_ownership_metadata


def render_panicking_domain(*, bzimage: str, disk: Path, console: Path) -> str:
    """Render the preserved gdbstub domain used by live early-panic tests.

    The rendered cmdline carries ``nokaslr`` so a symbol-addressed live debug command resolves
    against the running kernel's base rather than the vmlinux's link-time base (#711).

    Args:
        bzimage: Kernel image path to boot directly.
        disk: Empty disk path that causes the expected VFS panic.
        console: Serial console log path observed by the live-test harness.

    Returns:
        The rendered libvirt domain XML.
    """
    data = copy.deepcopy(PROFILE)
    section = data["provider"]["local-libvirt"]
    section["rootfs"] = {"kind": "local", "path": str(disk)}
    section["debug"] = {"gdbstub": True, "preserve_on_crash": True}
    section.pop("crashkernel", None)
    profile = ProvisioningProfile.parse(data)
    # ssh_port is required on every rendered domain (ADR-0281, #937) even though this panic-boot
    # never reaches sshd; pinned distinct from the gdb port. Omitting it raised CONFIGURATION_ERROR
    # before the boot (the #1255 live-proof gap).
    base = render_domain_xml(
        uuid4(),
        profile,
        disk_path=str(disk),
        gdb_port=51299,
        ssh_port=51298,
        kernel_path=Path(bzimage),
    )
    root = ET.fromstring(base)  # noqa: S314 - kdive-rendered, trusted
    drop_ownership_metadata(root)
    name = root.find("name")
    cmdline = root.find("./os/cmdline")
    assert name is not None and cmdline is not None and cmdline.text is not None
    name.text = "kdive-x"  # seed_system's domain_name, so the connector lookup resolves it
    # ``render_domain_xml`` already emitted <kernel> (this same bzimage) and the arch-resolved
    # baseline <cmdline>, so extend that element rather than appending a second pair whose
    # precedence is libvirt's to decide. ``panic=0`` holds the panicked guest for the gdbstub
    # instead of rebooting away from it. ``nokaslr`` pins the running kernel base to the fetched
    # vmlinux's link-time symbol addresses (#711): the baseline cmdline carries no gdbstub tokens
    # because ``nokaslr`` comes from the runs lane's ``system_required_cmdline``, which a
    # test-rendered transient domain never goes through, and without it a symbol-addressed
    # ``debug.disassemble`` reads unmapped memory and gdb/MI returns no instructions. The stepping
    # domain in test_debug_gdbmi_live_smoke.py appends the same token for the same reason.
    cmdline.text = f"{cmdline.text} panic=0 nokaslr"
    serial_log = root.find("./devices/serial/log")
    assert serial_log is not None
    serial_log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")
