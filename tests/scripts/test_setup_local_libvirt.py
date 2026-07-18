"""Behavioral tests for scripts/setup-local-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import scripts.kdive_set_accounting as acct

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-local-libvirt.sh"
BASH = shutil.which("bash")


def _helper_argv(logged: str) -> list[str]:
    """Extract the argv the script handed the helper from the stub's `echo "$@"` log."""
    line = next(line for line in logged.splitlines() if "scripts.kdive_set_accounting" in line)
    tokens = line.split()
    return tokens[tokens.index("scripts.kdive_set_accounting") + 1 :]


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _python_stub_body(calllog: Path) -> str:
    """python3 stub: succeed on the preflight `-c import` probe without logging it; log
    every other invocation (the onboarding helper) so tests can assert what ran."""
    return f'case "$*" in\n  -c*) exit 0 ;;\n  *) echo "$@" >> "{calllog}" ;;\nesac\nexit 0'


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
    # check-local-libvirt.sh also probes `python3 -c "import guestfs, drgn"`; that probe must
    # succeed (exit 0) but is not an onboarding call, so keep it out of the helper call log.
    _stub(bindir, "python3", _python_stub_body(calllog))
    # check-local-libvirt.sh requires a writable install-staging dir; provide one so the
    # preflight the setup script runs first passes.
    staging = tmp_path / "install-staging"
    staging.mkdir()
    # check-local-libvirt.sh also probes host-kernel readability (ADR-0222); point it at a
    # controlled dir with a readable kernel so the preflight passes regardless of the runner's
    # real /boot (Ubuntu CI ships root:0600 kernels, which would otherwise fail it).
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")
    # Stub bin first so it shadows real python3/virsh/etc.; system bins follow so the
    # scripts' `dirname` (and other coreutils) resolve. Point KDIVE_PYTHON at the stub
    # explicitly: check-local-libvirt.sh now prefers `.venv/bin/python` (present on
    # dev checkouts and on CI once `uv sync` runs) over the PATH `python3`, and that
    # venv interpreter does NOT have the stubbed `-c "import guestfs, drgn"` behavior.
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(boot),
        "KDIVE_PYTHON": str(bindir / "python3"),
    }
    return bindir, env, calllog


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_default_path_runs_seed_project(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "-m kdive seed-project" in logged
    assert "--project demo" in logged


def test_audited_path_runs_mcp_helper_not_seed_project(tmp_path: Path) -> None:
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
    assert "seed-project" not in logged


def test_audited_path_emits_argv_the_helper_accepts(tmp_path: Path) -> None:
    # Close the shell->argparse seam: the exact argv the script emits must parse with the
    # real helper (the script tests stub python3, so argparse never runs there otherwise).
    _bindir, env, calllog = _healthy_local(tmp_path)
    env |= {
        "KDIVE_SETUP_AUDITED": "1",
        "KDIVE_MCP_BASE": "http://localhost:8000/mcp",
        "KDIVE_TOKEN": "T",
    }
    result = _run(env)
    assert result.returncode == 0, result.stderr
    ns = acct.parse(_helper_argv(calllog.read_text()))
    assert ns.base == "http://localhost:8000/mcp"
    assert ns.project == "demo"
    assert ns.limit_kcu == "1000000"
    assert ns.max_alloc == 4
    assert ns.max_sys == 4


def test_audited_path_requires_token(tmp_path: Path) -> None:
    _bindir, env, calllog = _healthy_local(tmp_path)
    env |= {"KDIVE_SETUP_AUDITED": "1", "KDIVE_MCP_BASE": "http://localhost:8000/mcp"}
    # KDIVE_TOKEN intentionally unset: the audited path must fail up front.
    result = _run(env)
    assert result.returncode != 0
    # The helper must never run when the token guard fires.
    assert not calllog.exists()


def test_preflight_failure_aborts(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", _python_stub_body(calllog))
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(tmp_path / "absent"),
    }  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode != 0
    # Onboarding must not run when the preflight aborts.
    assert not calllog.exists()
