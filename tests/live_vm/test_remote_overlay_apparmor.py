"""Native Ubuntu proof for remote volume-backed overlays under AppArmor (ADR-0597)."""

from __future__ import annotations

import re
import subprocess
from contextlib import closing
from typing import cast
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.lifecycle.storage import (
    OverlayPool,
    cleanup_overlay_if_created,
    ensure_named_overlay,
)
from kdive.providers.remote_libvirt.lifecycle.xml import render_domain_xml
from tests.live_vm import require_live_vm_remote

_SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9_.@:-]+\Z")
_POOL = "default"


def _safe_ssh(destination: str, script: str, *tokens: str) -> subprocess.CompletedProcess[str]:
    """Run a fixed stdin script; only bounded tokens cross SSH's remote command string."""
    if not _SAFE_TOKEN.fullmatch(destination) or any(not _SAFE_TOKEN.fullmatch(x) for x in tokens):
        raise ValueError("remote proof identifiers must use the safe token alphabet")
    return subprocess.run(
        ["ssh", "--", destination, "sudo", "-n", "bash", "-s", "--", *tokens],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )


def test_safe_ssh_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError):
        _safe_ssh("operator@host", "exit 0", "name;touch-pwned")
    with pytest.raises(ValueError):
        _safe_ssh("operator@host$(id)", "exit 0", "safe")


def _profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://kernel.example/repo#v1",
            "provider": {
                "remote-libvirt": {
                    "base_image_volume": "operator-catalog",
                    "crashkernel": "256M",
                }
            },
        }
    )


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_overlay_boots_with_exact_apparmor_backing_grant() -> None:
    contract = require_live_vm_remote()
    run = f"kdive2236{uuid4().hex[:12]}"
    staged = f"{run}-staged.qcow2"
    supplied = f"{run}-supplied.qcow2"
    decoy = f"{run}-decoy.qcow2"
    overlay_name = f"{run}-overlay.qcow2"
    system_id = uuid4()
    domain = None
    overlay = None
    setup = r"""
base=$(virsh vol-path --pool default --vol "$2")
dir=${base%/*}
touch "$dir/$1-marker"
qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$dir/$1-staged.qcow2"
qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$dir/$1-supplied.qcow2"
qemu-img create -q -f qcow2 "$dir/$1-decoy.qcow2" 1M
virsh pool-refresh default >/dev/null
"""
    cleanup = r"""
base=$(virsh vol-path --pool default --vol "$2")
dir=${base%/*}
rm -f "$dir/$1-marker" "$dir/$1-staged.qcow2" "$dir/$1-supplied.qcow2" "$dir/$1-decoy.qcow2"
virsh pool-refresh default >/dev/null
"""
    try:
        _safe_ssh(contract.ssh_destination, setup, run, contract.base_image)
        with closing(libvirt.open(contract.libvirt_uri)) as conn:
            pool = conn.storagePoolLookupByName(_POOL)
            pool.refresh()
            pool.storageVolLookupByName(f"{run}-marker")
            overlay_pool = cast("OverlayPool", pool)
            for chained in (staged, supplied):
                with pytest.raises(CategorizedError) as caught:
                    ensure_named_overlay(overlay_pool, chained, f"{chained}-rejected-overlay")
                assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
                with pytest.raises(libvirt.libvirtError):
                    pool.storageVolLookupByName(f"{chained}-rejected-overlay")

            overlay = ensure_named_overlay(overlay_pool, contract.base_image, overlay_name)
            xml = render_domain_xml(
                system_id,
                _profile(),
                pool=_POOL,
                volume=overlay.name,
                backing_path=overlay.backing_path,
                gdb_addr="127.0.0.1",
                gdb_port=55997,
            )
            domain = conn.defineXML(xml)
            domain.create()
            if domain.isActive() != 1:
                raise AssertionError("remote domain did not remain active after start")
            profile = _safe_ssh(
                contract.ssh_destination,
                'cat "/etc/apparmor.d/libvirt/libvirt-$1.files"',
                str(system_id),
            ).stdout
            if overlay.backing_path not in profile:
                raise AssertionError("generated AppArmor profile omitted the selected base")
            pool_wildcard = f"{overlay.backing_path.rsplit('/', 1)[0]}/*"
            if decoy in profile or pool_wildcard in profile:
                raise AssertionError("generated AppArmor profile admitted an unrelated pool path")
    finally:
        if domain is not None:
            if domain.isActive() == 1:
                domain.destroy()
            domain.undefine()
        if overlay is not None:
            cleanup_overlay_if_created(cast("OverlayPool", pool), overlay)
        _safe_ssh(contract.ssh_destination, cleanup, run, contract.base_image)
