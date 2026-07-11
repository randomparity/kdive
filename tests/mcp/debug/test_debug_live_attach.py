"""Live end-to-end proof for #747 (ADR-0233): the real ``debug.start_session`` handler opens a
live gdbstub session against a real early-boot-panicked, preserved libvirt domain.

Kept in its own module (not ``test_debug_tools.py``) so the live marker does not make the
behaviour-test-coverage gate treat the ``debug.*`` covering test as live-only. Reuses the DB seed
helpers from ``test_debug_tools`` and the shared ``migrated_url`` Postgres fixture.

`live_vm`-gated: the operator points ``KDIVE_LIVE_VM_BZIMAGE`` at a kernel image that panics early
in boot (no usable rootfs), optionally overriding ``KDIVE_LIBVIRT_URI`` (default ``qemu:///session``
so it needs no root).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import SystemState
from kdive.mcp.tools.debug.sessions import lifecycle as debug_tools
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.debug.test_debug_tools import (
    _PROFILE,
    _PROFILE_POLICY,
    _ctx,
    _granted_allocation,
    _pool,
    _seed_run,
    _seed_system,
)
from tests.mcp.systems_support import provider_resolver


@pytest.mark.live_vm
def test_live_vm_start_session_attaches_to_halted_early_boot_crash(  # pragma: no cover
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real Postgres + the real start_session handler + the real LocalLibvirtConnect connector
    (resolves the gdb port from the live domain XML, runs the real rsp_reachable probe) open a
    live gdbstub session against a real KVM domain that VFS-panics on an empty disk. Only the
    boot-step row is seeded directly (its recording is unit-tested separately)."""
    bzimage = os.environ.get("KDIVE_LIVE_VM_BZIMAGE")
    if not bzimage or not Path(bzimage).is_file():
        pytest.skip("KDIVE_LIVE_VM_BZIMAGE (an early-panicking kernel image) unavailable")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    uri = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///session")
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", uri)
    disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    console.write_text("")
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(disk), "1G"], check=True, capture_output=True
    )

    final_xml = _render_panicking_domain(bzimage=bzimage, disk=disk, console=console)

    async def _drive() -> Any:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(
                pool, sys_id, boot_result={"boot_outcome": "crashed_halted_live"}
            )
            handlers = debug_tools.DebugSessionHandlers.from_resolver(
                provider_resolver(
                    connector=LocalLibvirtConnect.from_env(),
                    profile_policy=_PROFILE_POLICY,
                    supported_debug_transports=frozenset({"gdbstub"}),
                ),
                runtime_resolver=None,
                secret_registry=SecretRegistry(),
            )
            resp = await handlers.start_session(pool, _ctx(), run_id=run_id, transport="gdbstub")
            if resp.status == "live":
                await handlers.end_session(pool, _ctx(), resp.object_id)
            return resp

    conn = libvirt.open(uri)
    dom = None
    try:
        dom = conn.createXML(final_xml, 0)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if "Kernel panic" in console.read_text(errors="replace"):
                break
            time.sleep(0.5)
        assert "Kernel panic" in console.read_text(errors="replace"), "no early-boot panic"
        resp = asyncio.run(_drive())
        assert resp.status == "live", f"start_session did not attach: {resp.status} {resp.data}"
    finally:
        if dom is not None:
            with contextlib.suppress(libvirt.libvirtError):
                dom.destroy()
        conn.close()


def _render_panicking_domain(*, bzimage: str, disk: Path, console: Path) -> str:
    """kdive's real gdbstub+preserve domain XML, named to match _seed_system's domain_name and
    given the direct-kernel <os> (install.py's job) so it boots the kernel and VFS-panics."""
    data = copy.deepcopy(_PROFILE)
    section = data["provider"]["local-libvirt"]
    section["rootfs"] = {"kind": "local", "path": str(disk)}
    section["debug"] = {"gdbstub": True, "preserve_on_crash": True}
    section.pop("crashkernel", None)
    profile = ProvisioningProfile.parse(data)
    base = render_domain_xml(
        uuid4(), profile, disk_path=str(disk), gdb_port=51299, kernel_path=Path(bzimage)
    )
    root = ET.fromstring(base)  # noqa: S314 - kdive-rendered, trusted
    name_el = root.find("name")
    assert name_el is not None
    name_el.text = (
        "kdive-x"  # _seed_system's domain_name, so the connector's lookupByName resolves it
    )
    os_el = root.find("os")
    assert os_el is not None
    ET.SubElement(os_el, "kernel").text = bzimage
    ET.SubElement(os_el, "cmdline").text = "console=ttyS0 panic=0 root=/dev/vda"
    serial_log = root.find("./devices/serial/log")
    assert serial_log is not None
    serial_log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")
