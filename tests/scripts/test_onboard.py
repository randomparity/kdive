"""Behavioral tests for scripts/live-stack/onboard.sh via PATH stubs (#834).

Stubs `uv` (routes by the kdive subcommand / the mint heredoc) and the bins the real
check-local-libvirt.sh preflight probes, so the recipe's control flow — advisory preflight,
hard migrate/verify gates, the seed-fail-but-verified WARN, best-effort mint — is exercised
without a database, a libvirt host, or an OIDC issuer.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-stack" / "onboard.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _uv_stub_body(calllog: Path) -> str:
    """uv stub: log every invocation, route by subcommand/heredoc, honor *_FAIL env switches.

    The mint call is `uv run python - <project> <ttl> <role>` (a stdin script), distinct from the
    `uv run python -m kdive <cmd>` management calls; it prints a token to stdout on success.
    """
    return (
        f'echo "$@" >> "{calllog}"\n'
        'case "$*" in\n'
        '  *"-m kdive verify-project"*) [ -n "${VERIFY_FAIL:-}" ] && exit 1 ; exit 0 ;;\n'
        '  *"-m kdive seed-project"*) [ -n "${SEED_FAIL:-}" ] && exit 1 ; exit 0 ;;\n'
        '  *"python - "*) [ -n "${MINT_FAIL:-}" ] && exit 1 ; echo "FAKETOKEN" ; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac"
    )


def _healthy_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A PATH + env where the real check-local-libvirt.sh preflight passes and `uv` is stubbed."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    # check-local-libvirt.sh probes `python3 -c "import guestfs, drgn"`; succeed on it.
    _stub(bindir, "python3", 'case "$*" in -c*) exit 0 ;; esac\nexit 0')
    _stub(bindir, "uv", _uv_stub_body(tmp_path / "uv.log"))
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    staging = tmp_path / "install-staging"
    staging.mkdir()
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(boot),
    }
    return bindir, env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_happy_path_migrates_seeds_verifies_and_mints(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "run python -m kdive migrate" in logged
    assert "run python -m kdive seed-project --project demo" in logged
    assert "run python -m kdive verify-project --project demo" in logged
    assert "86400" in logged  # the mint heredoc TTL
    assert 'projects:["demo"]' in result.stdout
    assert 'roles:{"demo":"admin"}' in result.stdout
    assert 'project arg: "demo"' in result.stdout
    assert "export KDIVE_TOKEN=FAKETOKEN" in result.stdout


def test_project_override_threads_one_name(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["KDIVE_PROJECT"] = "acme"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "seed-project --project acme" in logged
    assert "verify-project --project acme" in logged
    assert 'projects:["acme"]' in result.stdout


def test_preflight_failure_is_advisory(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["KDIVE_KVM_NODE"] = str(tmp_path / "absent")  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "seed-project --project demo" in logged  # seed still ran
    assert "WARN" in result.stderr


def test_verify_failure_aborts_and_skips_mint(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["VERIFY_FAIL"] = "1"
    result = _run(env)
    assert result.returncode != 0
    logged = (tmp_path / "uv.log").read_text()
    assert "verify-project --project demo" in logged
    assert "python - " not in logged  # mint never ran


def test_seed_failure_with_verify_pass_warns_and_continues(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["SEED_FAIL"] = "1"  # discovery-style failure after the rows committed
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "python - " in logged  # mint still ran
    assert "WARN" in result.stderr
    assert "export KDIVE_TOKEN=FAKETOKEN" in result.stdout


def test_mint_failure_is_advisory(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["MINT_FAIL"] = "1"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "export KDIVE_TOKEN=FAKETOKEN" not in result.stdout
    assert "WARN" in result.stderr
    assert 'projects:["demo"]' in result.stdout  # contract still printed


def test_sub_contributor_role_warns(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["KDIVE_ROLE"] = "viewer"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "contributor" in result.stderr
    assert 'roles:{"demo":"viewer"}' in result.stdout
