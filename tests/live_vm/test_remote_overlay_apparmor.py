"""Native Ubuntu proof for remote volume-backed overlays under AppArmor (ADR-0597)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.lifecycle.readiness import wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.storage import (
    OverlayPool,
    ensure_named_overlay,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    render_domain_xml,
    supplied_base_volume_name,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.live_vm import LIVE_VM_REMOTE_SSH_ENV, require_live_vm_remote

_SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9_.@:-]+\Z")
_POOL = "default"


class _CleanupDomain(Protocol):
    def isActive(self) -> int: ...  # noqa: N802
    def destroy(self) -> object: ...
    def undefine(self) -> object: ...


def _safe_ssh(destination: str, script: str, *tokens: str) -> subprocess.CompletedProcess[str]:
    """Run a fixed stdin script; only bounded tokens cross SSH's remote command string."""
    if not _SAFE_TOKEN.fullmatch(destination) or any(not _SAFE_TOKEN.fullmatch(x) for x in tokens):
        raise ValueError("remote proof identifiers must use the safe token alphabet")
    try:
        return subprocess.run(
            ["ssh", "--", destination, "sudo", "-n", "bash", "-s", "--", *tokens],
            input=script,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError, subprocess.TimeoutExpired:
        raise AssertionError("remote proof control command failed (endpoint redacted)") from None


def _require_ssh_destination() -> str:
    destination = os.environ.get(LIVE_VM_REMOTE_SSH_ENV)
    if destination is None:
        pytest.skip(f"{LIVE_VM_REMOTE_SSH_ENV} unset; AppArmor control carrier unavailable")
    if not _SAFE_TOKEN.fullmatch(destination):
        pytest.fail(f"{LIVE_VM_REMOTE_SSH_ENV} uses characters outside the safe token alphabet")
    return destination


def _cleanup_remote_proof(
    domain: _CleanupDomain | None,
    ssh_destination: str,
    cleanup_script: str,
    *tokens: str,
) -> None:
    failures: list[str] = []
    if domain is not None:
        try:
            if domain.isActive() == 1:
                domain.destroy()
        except libvirt.libvirtError:
            failures.append("domain destroy")
        try:
            domain.undefine()
        except libvirt.libvirtError:
            failures.append("domain undefine")
    try:
        _safe_ssh(ssh_destination, cleanup_script, *tokens)
    except AssertionError:
        failures.append("remote files")
    if failures:
        raise AssertionError("remote proof cleanup failed: " + ", ".join(failures))


def test_safe_ssh_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError):
        _safe_ssh("operator@host", "exit 0", "name;touch-pwned")
    with pytest.raises(ValueError):
        _safe_ssh("operator@host$(id)", "exit 0", "safe")


def test_safe_ssh_redacts_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.CalledProcessError(255, ["ssh", "operator@private-host"], "secret")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(AssertionError) as caught:
        _safe_ssh("operator@private-host", "exit 1", "private-token")
    rendered = str(caught.value)
    assert "private-host" not in rendered
    assert "private-token" not in rendered
    assert "secret" not in rendered


def test_cleanup_attempts_undefine_and_remote_files_after_destroy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Domain:
        undefined = False

        def isActive(self) -> int:  # noqa: N802
            return 1

        def destroy(self) -> None:
            raise libvirt.libvirtError("failed")

        def undefine(self) -> None:
            self.undefined = True

    domain = Domain()
    called = False

    def succeed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        del args, kwargs
        called = True
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(subprocess, "run", succeed)
    with pytest.raises(AssertionError, match="domain destroy"):
        _cleanup_remote_proof(domain, "operator@host", "exit 0", "safe")
    assert domain.undefined
    assert called


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


def _supplied_profile(path: Path) -> ProvisioningProfile:
    data = _profile().model_dump(mode="json", by_alias=True)
    data["provider"]["remote-libvirt"].pop("base_image_volume")
    data["provider"]["remote-libvirt"]["base_image_source"] = {
        "kind": "local",
        "path": str(path),
    }
    return ProvisioningProfile.parse(data)


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_overlay_boots_with_exact_apparmor_backing_grant(tmp_path: Path) -> None:
    contract = require_live_vm_remote()
    ssh_destination = _require_ssh_destination()
    run = f"kdive2236{uuid4().hex[:12]}"
    staged = f"{run}-staged.qcow2"
    supplied_system_id = uuid4()
    supplied = supplied_base_volume_name(supplied_system_id)
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
qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$dir/$3"
qemu-img create -q -f qcow2 "$dir/$1-decoy.qcow2" 1M
virsh pool-refresh default >/dev/null
"""
    cleanup = r"""
base=$(virsh vol-path --pool default --vol "$2")
dir=${base%/*}
rm -f "$dir/$1-marker" "$dir/$1-staged.qcow2" "$dir/$3" \
  "$dir/$1-decoy.qcow2" "$dir/$1-overlay.qcow2"
virsh pool-refresh default >/dev/null
test ! -e "$dir/$1-marker"
test ! -e "$dir/$1-staged.qcow2"
test ! -e "$dir/$3"
test ! -e "$dir/$1-decoy.qcow2"
test ! -e "$dir/$1-overlay.qcow2"
"""
    check_overlay_dac = r"""
base=$(virsh vol-path --pool default --vol "$2")
dir=${base%/*}
overlay="$dir/$1-overlay.qcow2"
test "$(stat -c %a "$overlay")" = 600
test "$(stat -c %u "$overlay")" = "$(stat -c %u "$base")"
test "$(stat -c %g "$overlay")" = "$(stat -c %g "$base")"
sudo -u libvirt-qemu test -r "$overlay"
sudo -u libvirt-qemu test -w "$overlay"
"""
    try:
        _safe_ssh(ssh_destination, setup, run, contract.base_image, supplied)
        with closing(libvirt.open(contract.libvirt_uri)) as conn:
            pool = conn.storagePoolLookupByName(_POOL)
            pool.refresh()
            pool.storageVolLookupByName(f"{run}-marker")
            overlay_pool = cast("OverlayPool", pool)
            with pytest.raises(CategorizedError) as caught:
                ensure_named_overlay(overlay_pool, staged, f"{staged}-rejected-overlay")
            assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR

            replacement = tmp_path / "replacement.qcow2"
            replacement.write_bytes(b"QFI\xfbchanged-worker-source")
            provisioner = RemoteLibvirtProvisioning(
                secret_registry=SecretRegistry(), allowed_roots=(tmp_path,)
            )
            config = RemoteLibvirtConfig(
                uri=contract.libvirt_uri,
                cert_refs=TlsCertRefs("cert", "key", "ca"),
                concurrent_allocation_cap=1,
            )
            supplied_profile = _supplied_profile(replacement)
            section = supplied_profile.provider.remote_libvirt_section
            assert section is not None
            resolved, created = provisioner._resolve_base_volume(  # noqa: SLF001
                cast("Any", conn), section, supplied_system_id, config
            )
            assert (resolved, created) == (supplied, False)
            with pytest.raises(CategorizedError) as caught:
                ensure_named_overlay(overlay_pool, resolved, f"{supplied}-rejected-overlay")
            assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
            for rejected in (f"{staged}-rejected-overlay", f"{supplied}-rejected-overlay"):
                with pytest.raises(libvirt.libvirtError):
                    pool.storageVolLookupByName(rejected)

            overlay = ensure_named_overlay(overlay_pool, contract.base_image, overlay_name)
            _safe_ssh(ssh_destination, check_overlay_dac, run, contract.base_image, supplied)
            xml = render_domain_xml(
                system_id,
                _profile(),
                pool=_POOL,
                volume=overlay.name,
                overlay_path=overlay.path,
                backing_path=overlay.backing_path,
                gdb_addr="127.0.0.1",
                gdb_port=55997,
            )
            domain = conn.defineXML(xml)
            domain.create()
            wait_for_agent(
                conn,
                domain_name_for(system_id),
                monotonic=time.monotonic,
                sleep=time.sleep,
                timeout_s=120,
                poll_s=1,
            )
            profile = _safe_ssh(
                ssh_destination,
                'cat "/etc/apparmor.d/libvirt/libvirt-$1.files"',
                str(system_id),
            ).stdout
            if overlay.backing_path not in profile:
                raise AssertionError("generated AppArmor profile omitted the selected base")
            pool_wildcard = f"{overlay.backing_path.rsplit('/', 1)[0]}/*"
            if decoy in profile or pool_wildcard in profile:
                raise AssertionError("generated AppArmor profile admitted an unrelated pool path")
    finally:
        primary = sys.exception()
        try:
            _cleanup_remote_proof(
                domain, ssh_destination, cleanup, run, contract.base_image, supplied
            )
        except AssertionError as cleanup_error:
            if isinstance(primary, Exception):
                raise ExceptionGroup(
                    "remote proof and cleanup both failed", [primary, cleanup_error]
                ) from None
            raise
