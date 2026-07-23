"""preflight-env.sh fails loud on a declared family's missing env (#1293, ADR-0389).

A declared family with absent env must FAIL the job (non-zero, names the missing var) — never a
green skip. Mirrors the subprocess-invocation pattern of tests/scripts/test_live_vm_stores.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-vm" / "preflight-env.sh"
# Absolute bash so the child launches even when a test strips PATH (the tcg emulator-absent case).
_BASH = shutil.which("bash") or "/usr/bin/bash"


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {"PATH": os.environ["PATH"], **env}
    return subprocess.run([_BASH, str(_SCRIPT), *args], capture_output=True, text=True, env=full)


def test_throwaway_ok_when_rootfs_exists(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    r = _run(
        ["throwaway"],
        {"KDIVE_LIVE_VM_ROOTFS": str(rootfs), "KDIVE_LIBVIRT_URI": "qemu:///session"},
    )
    assert r.returncode == 0, r.stderr


def test_throwaway_fails_when_rootfs_missing() -> None:
    r = _run(["throwaway"], {"KDIVE_LIBVIRT_URI": "qemu:///session"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_provisioned_fails_without_system_id() -> None:
    r = _run(["provisioned"], {"KDIVE_S3_ENDPOINT_URL": "http://x", "KDIVE_S3_BUCKET": "b"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_SYSTEM_ID" in r.stderr


def test_tcg_fails_without_ppc64le_emulator(tmp_path: Path) -> None:
    img = tmp_path / "img.qcow2"
    img.write_bytes(b"x")
    tree = tmp_path / "linux"
    tree.mkdir()
    # A minimal PATH with the coreutils the script's bootstrap needs (dirname/pwd) but NOT
    # qemu-system-ppc64, so the emulator check fails without breaking the script's own launch.
    minimal_bin = tmp_path / "bin"
    minimal_bin.mkdir()
    for tool in ("dirname", "pwd", "env"):
        real = shutil.which(tool)
        if real:
            (minimal_bin / tool).symlink_to(real)
    env = {
        "KDIVE_STACK_BASE_URL": "http://x",
        "KDIVE_OIDC_ISSUER": "http://x",
        "KDIVE_DATABASE_URL": "postgresql://x",
        "KDIVE_S3_ENDPOINT_URL": "http://x",
        "KDIVE_S3_BUCKET": "b",
        "AWS_ACCESS_KEY_ID": "k",
        "AWS_SECRET_ACCESS_KEY": "s",
        "KDIVE_GUEST_IMAGE_PPC64LE": str(img),
        "KDIVE_KERNEL_SRC": str(tree),
        "PATH": str(minimal_bin),  # coreutils present, no qemu-system-ppc64
    }
    r = _run(["tcg"], env)
    assert r.returncode != 0
    assert "qemu-system-ppc64" in r.stderr


def test_unknown_family_fails_loud() -> None:
    r = _run(["bogus"], {})
    assert r.returncode != 0
    assert "bogus" in r.stderr


def test_no_family_arg_fails_loud() -> None:
    r = _run([], {})
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()


# ---------------------------------------------------------------------------
# host family: the libvirt/KVM contract build-fs needs BEFORE it starts building
# ---------------------------------------------------------------------------
#
# build-fs boots a customization guest through libvirt, so a missing daemon or an unreachable
# /dev/kvm only surfaced minutes into a multi-GB build (as `resolve_accel` blowing up on
# "Failed to connect socket to /var/run/libvirt/libvirt-sock"). The host family front-loads that.


def _host_bin(tmp_path: Path, *, virsh_rc: int = 0, with_qemu_img: bool = True) -> Path:
    """A PATH holding the coreutils the script needs plus stubbed host tools."""
    bindir = tmp_path / "hostbin"
    bindir.mkdir()
    for tool in ("dirname", "pwd", "env", "id"):
        real = shutil.which(tool)
        if real:
            (bindir / tool).symlink_to(real)
    (bindir / "virsh").write_text(f"#!/bin/sh\nexit {virsh_rc}\n")
    (bindir / "virsh").chmod(0o755)
    if with_qemu_img:
        (bindir / "qemu-img").write_text("#!/bin/sh\nexit 0\n")
        (bindir / "qemu-img").chmod(0o755)
    return bindir


def test_host_ok_when_libvirt_connects_and_kvm_is_usable(tmp_path: Path) -> None:
    bindir = _host_bin(tmp_path)
    env = {
        "PATH": str(bindir),
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_KVM_NODE": "/dev/null",  # readable+writable stand-in for /dev/kvm
        "KDIVE_HOST_RUNTIME_DIRS": str(tmp_path),  # provisioned; the dir cases cover the gap
    }
    r = _run(["host"], env)
    assert r.returncode == 0, r.stderr


def test_host_fails_when_libvirt_is_unreachable(tmp_path: Path) -> None:
    """The exact gap that cost a 7-minute build: libvirt-daemon absent, so virsh cannot connect."""
    bindir = _host_bin(tmp_path, virsh_rc=1)
    env = {
        "PATH": str(bindir),
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_KVM_NODE": "/dev/null",
        "KDIVE_HOST_RUNTIME_DIRS": str(tmp_path),
    }
    r = _run(["host"], env)
    assert r.returncode != 0
    assert "qemu:///session" in r.stderr  # names the URI it could not reach
    assert "libvirt" in r.stderr.lower()


def test_host_fails_when_kvm_node_is_not_usable(tmp_path: Path) -> None:
    bindir = _host_bin(tmp_path)
    env = {
        "PATH": str(bindir),
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_KVM_NODE": str(tmp_path / "absent-kvm"),
        "KDIVE_HOST_RUNTIME_DIRS": str(tmp_path),
    }
    r = _run(["host"], env)
    assert r.returncode != 0
    assert "kvm" in r.stderr.lower()


def test_host_fails_without_a_libvirt_uri(tmp_path: Path) -> None:
    """No silent default: qemu:///system and qemu:///session have different readback contracts."""
    bindir = _host_bin(tmp_path)
    r = _run(["host"], {"PATH": str(bindir), "KDIVE_KVM_NODE": "/dev/null"})
    assert r.returncode != 0
    assert "KDIVE_LIBVIRT_URI" in r.stderr


# ---------------------------------------------------------------------------
# host family: runtime directories the provider hardcodes
# ---------------------------------------------------------------------------


def _script_runtime_dirs() -> list[str]:
    """The default KDIVE_HOST_RUNTIME_DIRS list the script declares."""
    line = next(ln for ln in _SCRIPT.read_text().splitlines() if "KDIVE_HOST_RUNTIME_DIRS:-" in ln)
    return line.split("KDIVE_HOST_RUNTIME_DIRS:-", 1)[1].split("}", 1)[0].split()


def test_host_runtime_dirs_cover_every_hardcoded_provider_path() -> None:
    """Drift guard: these paths are constants in src, so they cannot be pointed elsewhere.

    A non-root worker cannot create them under root-owned /var/lib/kdive, so each must be
    provisioned ahead of the run. If someone adds another hardcoded runtime dir, this fails in PR
    CI rather than several minutes into a live gate — which is how the console dir was found.
    Reads the private constants deliberately: they are precisely what the deployment must match.
    """
    from kdive.providers.local_libvirt.lifecycle import storage
    from kdive.providers.shared import runtime_paths

    declared = set(_script_runtime_dirs())
    for const in (runtime_paths._CONSOLE_DIR, runtime_paths._PCAP_DIR, storage.ROOTFS_DIR):
        assert const in declared, f"{const} is hardcoded in src but not preflighted"


def test_host_fails_when_a_runtime_dir_is_not_writable(tmp_path: Path) -> None:
    bindir = _host_bin(tmp_path)
    absent = tmp_path / "not-provisioned"
    env = {
        "PATH": str(bindir),
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_KVM_NODE": "/dev/null",
        "KDIVE_HOST_RUNTIME_DIRS": str(absent),
    }
    r = _run(["host"], env)
    assert r.returncode != 0
    assert str(absent) in r.stderr


def test_host_ok_when_runtime_dirs_are_writable(tmp_path: Path) -> None:
    bindir = _host_bin(tmp_path)
    good = tmp_path / "console"
    good.mkdir()
    env = {
        "PATH": str(bindir),
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_KVM_NODE": "/dev/null",
        "KDIVE_HOST_RUNTIME_DIRS": str(good),
    }
    r = _run(["host"], env)
    assert r.returncode == 0, r.stderr
