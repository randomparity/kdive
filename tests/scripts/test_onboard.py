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

import pytest

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
    # check-local-libvirt.sh derives the REQUIRED native emulator from `uname -m` — x86_64 wants
    # qemu-system-x86_64 (stubbed above), ppc64le wants qemu-system-ppc64 (not). Unstubbed, the
    # fixture's verdict is decided by the machine: on a POWER host the probe fails, and on x86_64
    # it can still be satisfied by whatever qemu the host happens to have installed.
    _stub(bindir, "uname", 'case "$*" in -m) echo x86_64 ;; *) echo Linux ;; esac')
    # check-local-libvirt.sh probes `python3 -c "import guestfs, drgn"`; succeed on it.
    _stub(bindir, "python3", 'case "$*" in -c*) exit 0 ;; esac\nexit 0')
    _stub(bindir, "uv", _uv_stub_body(tmp_path / "uv.log"))
    # The SELinux label probe reads the host's real getenforce off /usr/bin; pin it so an
    # enforcing developer host does not fail the fixture's unlabeled tmp dirs (ADR-0640).
    _stub(bindir, "getenforce", "echo Disabled")
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
        # The RedHat off-PATH emulator fallback, pinned absent so it cannot resolve from the host.
        "KDIVE_QEMU_LIBEXEC": str(tmp_path / "absent-qemu-kvm"),
        # check-local-libvirt.sh prefers the REPO .venv over the stubbed python3 when one exists,
        # and that venv has no guestfs/drgn — so without this the preflight's verdict depends on
        # whether the checkout has been synced. Downgrade the one probe with a documented soft
        # case (the same knob demo-up.sh uses) so "healthy" means healthy on any machine. It is
        # the only one of the nine blocking checks downgraded here, so a `required` case in this
        # file proves the gate against 8/9 real gates plus this one as a WARN.
        "KDIVE_PREFLIGHT_KDUMP": "optional",
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
    assert "2592000" in logged  # the mint heredoc TTL, defaulted from live-stack/env.sh (30d)
    assert 'projects:["demo"]' in result.stdout
    assert 'roles:{"demo":"admin"}' in result.stdout
    assert 'project arg: "demo"' in result.stdout
    assert "expires in 30d" in result.stdout  # TTL rendered human-readable, not "720h"
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


def test_token_ttl_override_is_threaded_and_rendered_in_hours(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["KDIVE_TOKEN_TTL"] = "3600"  # a sub-day override exercises the hours branch
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "3600" in logged  # override threaded into the mint call
    assert "expires in 1h" in result.stdout  # not "1d", not a bare second count


def test_preflight_failure_is_advisory(tmp_path: Path) -> None:
    _bindir, env = _healthy_env(tmp_path)
    env["KDIVE_KVM_NODE"] = str(tmp_path / "absent")  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "seed-project --project demo" in logged  # seed still ran
    assert "WARN" in result.stderr


@pytest.mark.parametrize("declared", ["advisory", ""])
def test_explicit_advisory_matches_the_default(tmp_path: Path, declared: str) -> None:
    """`advisory` and empty are the documented default, not a second behaviour (ADR-0666)."""
    _bindir, env = _healthy_env(tmp_path)
    env["ONBOARD_PREFLIGHT"] = declared
    env["KDIVE_KVM_NODE"] = str(tmp_path / "absent")  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert "seed-project --project demo" in (tmp_path / "uv.log").read_text()


def test_required_aborts(tmp_path: Path) -> None:
    """ONBOARD_PREFLIGHT=required stops before migrate, attributing the stop (#2568)."""
    _bindir, env = _healthy_env(tmp_path)
    env["ONBOARD_PREFLIGHT"] = "required"
    env["KDIVE_KVM_NODE"] = str(tmp_path / "absent")  # unreadable -> preflight fails
    result = _run(env)
    assert result.returncode != 0
    # The preflight's own diagnosis survives as the reason, not a generic later failure.
    assert "KVM unavailable" in result.stderr
    assert "ONBOARD_PREFLIGHT=required" in result.stderr
    # Nothing past the gate ran: the log has no migrate, and the funding steps never started.
    logged = (tmp_path / "uv.log").read_text() if (tmp_path / "uv.log").exists() else ""
    assert "migrate" not in logged
    assert "seed-project" not in logged


def test_required_aborts_on_unreachable_configured_endpoint(tmp_path: Path) -> None:
    bindir, env = _healthy_env(tmp_path)
    _stub(bindir, "virsh", 'case "$*" in *qemu:///system*) echo "Active: yes";; *) exit 1;; esac')
    env["KDIVE_LIBVIRT_URI"] = "qemu:///session"
    env["ONBOARD_PREFLIGHT"] = "required"

    result = _run(env)
    assert result.returncode == 1
    assert "cannot connect" in result.stderr
    assert "KDIVE_LIBVIRT_URI" in result.stderr
    assert "ONBOARD_PREFLIGHT=required" in result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_required_proceeds(tmp_path: Path) -> None:
    """A passing preflight is not blocked: the gate reads the exit status, not its own mode."""
    _bindir, env = _healthy_env(tmp_path)
    env["ONBOARD_PREFLIGHT"] = "required"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    logged = (tmp_path / "uv.log").read_text()
    assert "run python -m kdive migrate" in logged
    assert "export KDIVE_TOKEN=FAKETOKEN" in result.stdout


def test_invalid_preflight_refused(tmp_path: Path) -> None:
    """A typo is an error, not a silent fallback to advisory (mirrors KDIVE_PREFLIGHT_KDUMP)."""
    _bindir, env = _healthy_env(tmp_path)
    env["ONBOARD_PREFLIGHT"] = "requried"
    result = _run(env)
    assert result.returncode != 0
    assert "ONBOARD_PREFLIGHT=requried is not valid" in result.stderr
    logged = (tmp_path / "uv.log").read_text() if (tmp_path / "uv.log").exists() else ""
    assert "migrate" not in logged


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
