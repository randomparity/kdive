"""Behavioral tests for scripts/setup-remote-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-remote-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _healthy_remote(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "ssh", "exit 0")
    _stub(bindir, "virsh", "exit 0")
    calllog = tmp_path / "python.log"
    _stub(bindir, "python3", f'echo "$@" >> "{calllog}"\nexit 0')
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "clientcert.pem").write_text("x")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "kdive-agent").write_text("x")
    env = {
        # Stub bin first (shadows ssh/virsh/python3); system bins follow so `dirname` resolves.
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_REMOTE_PKI_DIR": str(pki),
        "KDIVE_GUEST_HELPERS_DIR": str(helpers),
        "KDIVE_TOKEN": "T",
        "KDIVE_MCP_BASE": "http://127.0.0.1:8000/mcp",
    }
    return env, calllog


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def test_onboards_via_mcp_helper(tmp_path: Path) -> None:
    env, calllog = _healthy_remote(tmp_path)
    result = _run(env, "target.example", "root")
    assert result.returncode == 0, result.stderr
    logged = calllog.read_text()
    assert "kdive_set_accounting" in logged
    assert "--base http://127.0.0.1:8000/mcp" in logged


def test_missing_host_arg_fails(tmp_path: Path) -> None:
    env, _calllog = _healthy_remote(tmp_path)
    result = _run(env)  # no HOST
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
