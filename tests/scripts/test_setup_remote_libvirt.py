"""Behavioral tests for scripts/setup-remote-libvirt.sh via PATH stubs."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import scripts.kdive_set_accounting as acct

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-remote-libvirt.sh"
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
        # Pin KDIVE_PYTHON at the stub: the script now prefers the repo `.venv/bin/python`
        # (present on dev checkouts and on CI once `uv sync` runs) over the PATH `python3`,
        # and that real venv interpreter would run the onboarding helper for real (#1328).
        "KDIVE_PYTHON": str(bindir / "python3"),
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


def test_emits_argv_the_helper_accepts(tmp_path: Path) -> None:
    # Close the shell->argparse seam: the exact argv the script emits must parse with the
    # real helper (the script tests stub python3, so argparse never runs there otherwise).
    env, calllog = _healthy_remote(tmp_path)
    result = _run(env, "target.example", "root")
    assert result.returncode == 0, result.stderr
    ns = acct.parse(_helper_argv(calllog.read_text()))
    assert ns.base == "http://127.0.0.1:8000/mcp"
    assert ns.project == "demo"
    assert ns.limit_kcu == "1000000"
    assert ns.max_alloc == 4
    assert ns.max_sys == 4


def test_autodetects_repo_venv_over_system_python3(tmp_path: Path) -> None:
    """With KDIVE_PYTHON unset, onboarding runs under the repo .venv, not system python3 (#1328).

    Runs a copy of the script from a temp repo carrying a fake .venv/bin/python sibling, with a
    distinct system python3 on PATH. Only the venv interpreter must run the onboarding helper.
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / "setup-remote-libvirt.sh")
    _stub(scripts, "check-remote-libvirt.sh", "exit 0")  # preflight passes; not under test here
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_log = tmp_path / "venv.log"
    _stub(venv_bin, "python", f'echo "$@" >> "{venv_log}"\nexit 0')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    system_log = tmp_path / "system.log"
    _stub(bindir, "python3", f'echo "$@" >> "{system_log}"\nexit 0')

    assert BASH is not None
    result = subprocess.run(
        # HOST arg required; KDIVE_TOKEN set so demo-token.sh is skipped; KDIVE_PYTHON unset.
        [BASH, str(scripts / "setup-remote-libvirt.sh"), "target.example", "root"],
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "KDIVE_TOKEN": "T",
            "KDIVE_MCP_BASE": "http://127.0.0.1:8000/mcp",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert venv_log.exists(), "onboarding did not run under the repo .venv interpreter"
    assert "kdive_set_accounting" in venv_log.read_text()
    assert not system_log.exists(), "onboarding fell back to system python3 despite a present .venv"


def test_missing_host_arg_fails(tmp_path: Path) -> None:
    env, _calllog = _healthy_remote(tmp_path)
    result = _run(env)  # no HOST
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
