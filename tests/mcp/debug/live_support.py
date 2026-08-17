"""Shared domain rendering for live debug tests."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from tests.mcp.debug.session_support import PROFILE


def drop_ownership_metadata(root: ET.Element) -> None:
    """Strip the kdive ownership tag from a transient, non-production domain (#1930).

    ``render_domain_xml`` always stamps ``<metadata><kdive:system>`` with the System id it was
    handed, because every domain it renders in production *is* owned by a live ``systems`` row.
    The live-debug helpers below render that production XML for a System that exists only in
    pytest's disposable database, so the tag is a false ownership claim: a concurrently running
    production reconciler resolves it (the tag is authoritative in
    ``LocalLibvirtDiscovery._owned_entry``), finds no matching row, and ``repair_leaked_domains``
    destroys the domain mid-test.

    Dropping the whole ``<metadata>`` element — kdive renders nothing else inside it — sends
    ``_owned_entry`` down its ``VIR_ERR_NO_DOMAIN_METADATA`` path, where the ``kdive-x`` name
    these helpers set does not match the ``kdive-<uuid>`` convention and is correctly treated as
    foreign/unmanaged. Everything else stays production XML: the gdbstub passthrough, the SSH
    forward, the direct-kernel ``<os>``, and the disk are exactly what a real System boots.
    """
    metadata = root.find("metadata")
    if metadata is not None:
        root.remove(metadata)


def render_panicking_domain(*, bzimage: str, disk: Path, console: Path) -> str:
    """Render the preserved gdbstub domain used by live early-panic tests.

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
    assert name is not None
    name.text = "kdive-x"  # seed_system's domain_name, so the connector lookup resolves it
    os_element = root.find("os")
    assert os_element is not None
    ET.SubElement(os_element, "kernel").text = bzimage
    ET.SubElement(os_element, "cmdline").text = "console=ttyS0 panic=0 root=/dev/vda"
    serial_log = root.find("./devices/serial/log")
    assert serial_log is not None
    serial_log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")
