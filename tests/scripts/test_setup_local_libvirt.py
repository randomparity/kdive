"""Behavioral tests for scripts/setup-local-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _healthy_local(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Stubs that make the REAL check-local-libvirt.sh pass under this PATH.
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    # Stub bin first so it shadows real python3/virsh/etc.; system bins follow so the
    # scripts' `dirname` (and other coreutils) resolve.
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    return bindir, env, calllog


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_default_path_runs_seed_demo(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "-m kdive seed-demo" in logged
    assert "--project demo" in logged


def test_audited_path_runs_mcp_helper_not_seed_demo(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    env |= {
        "KDIVE_SETUP_AUDITED": "1",
        "KDIVE_MCP_BASE": "http://localhost:8000/mcp",
        "KDIVE_TOKEN": "T",
    }
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "kdive_set_accounting" in logged
    assert "seed-demo" not in logged


def test_preflight_failure_aborts(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(tmp_path / "absent"),
    }  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode != 0
    # Onboarding must not run when the preflight aborts.
    assert not calllog.exists()
