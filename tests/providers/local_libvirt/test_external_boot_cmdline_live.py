"""Native live proof for the local external-boot command-line observation (#2175)."""

from __future__ import annotations

import socket
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms import LocalRunningObserver
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.ports.external_boot import RunningKernelObservation
from kdive.testing.live_vm import boot_gdbstub_domain, create_overlay
from tests.live_vm import require_live_vm_bzimage, require_live_vm_throwaway
from tests.support.domain_ownership import drop_ownership_metadata


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_local_observer_returns_exact_running_cmdline() -> None:
    rootfs = require_live_vm_throwaway("qemu:///system")
    kernel = require_live_vm_bzimage("qemu:///system")
    overlay = rootfs.rootfs.with_name(f"kdive-cmdline-live-{uuid4().hex[:12]}.qcow2")
    console = overlay.with_suffix(".log")
    create_overlay(rootfs.rootfs, overlay)
    try:
        system_id = uuid4()
        xml = _transient_xml(
            system_id,
            overlay,
            kernel.bzimage,
            _optional_initramfs(kernel.bzimage),
            _free_port(),
            console,
        )
        with boot_gdbstub_domain(xml, uri=rootfs.libvirt_uri, wait_for="active") as live:
            observation = _await_observation(LocalRunningObserver(), system_id, live.domain)

        expected = ET.fromstring(xml).findtext("./os/cmdline")
        assert expected is not None
        assert observation.cmdline == observation.expected_cmdline == expected.encode()
    finally:
        overlay.unlink(missing_ok=True)
        console.unlink(missing_ok=True)


def _transient_xml(
    system_id: UUID,
    disk: Path,
    kernel: Path,
    initramfs: Path | None,
    ssh_port: int,
    console: Path,
) -> str:
    profile = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 1024,
            "disk_gb": 6,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://kernel.invalid/linux.git#live-proof",
            "provider": {
                "local-libvirt": {
                    "domain_xml_params": {"machine": "pc-q35-9.0"},
                    "rootfs": {"kind": "local", "path": str(disk)},
                    "debug": {"gdbstub": False, "preserve_on_crash": False},
                }
            },
        }
    )
    xml = render_domain_xml(
        system_id,
        profile,
        disk_path=str(disk),
        ssh_port=ssh_port,
        kernel_path=kernel,
        initrd_path=initramfs,
    )
    root = ET.fromstring(xml)
    drop_ownership_metadata(root)
    name = root.find("name")
    assert name is not None
    name.text = f"kdive-cmdline-live-{uuid4().hex[:12]}"
    log = root.find("./devices/serial/log")
    assert log is not None
    console.touch()
    log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")


def _optional_initramfs(kernel: Path) -> Path | None:
    candidate = kernel.with_name("initramfs")
    return candidate if candidate.is_file() else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _await_observation(
    observer: LocalRunningObserver, system_id: UUID, domain: Any
) -> RunningKernelObservation:
    deadline = time.monotonic() + 60
    while True:
        try:
            return observer(system_id, domain)
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
