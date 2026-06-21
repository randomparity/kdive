"""Behavioral tests for scripts/check-setup-deps.sh.

The script's value is its per-distro install hints, so these tests drive it with a
synthetic os-release (via KDIVE_OS_RELEASE) and a controlled PATH, then assert the
package names and exit status for each distro family. The script uses only shell
builtins, so an empty PATH makes every external dependency look missing without
needing stubs.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-setup-deps.sh"
BASH = shutil.which("bash")

# The script collects required-tool reports through a `local -n` nameref (bash >= 4.3).
pytestmark = requires_bash(4, 3, "local -n namerefs")


def _run(os_release_id: str, path: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker with a forced distro and a controlled PATH."""
    assert BASH is not None, "bash is required to run the checker"
    os_release = tmp_path / "os-release"
    os_release.write_text(f"ID={os_release_id}\n")
    env = {
        "PATH": path,
        "KDIVE_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        [BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("distro_id", "manager", "libvirt_package"),
    [
        ("fedora", "dnf install", "libvirt-devel"),
        ("debian", "apt install", "libvirt-dev"),
        ("arch", "pacman -S", "libvirt"),
        ("opensuse", "zypper install", "libvirt-devel"),
    ],
)
def test_all_missing_emits_required_hint_per_distro(
    distro_id: str, manager: str, libvirt_package: str, tmp_path: Path
) -> None:
    """With nothing on PATH, the required tier names the right package manager."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run(distro_id, str(empty), tmp_path)

    assert result.returncode == 1, result.stderr
    assert "Required dependencies missing" in result.stderr
    assert manager in result.stderr
    assert libvirt_package in result.stderr
    # Tools the distro does not package are routed to manual hints, not the
    # package-manager line.
    assert "uv tool install prek" in result.stderr
    assert f"{manager} prek" not in result.stderr


def test_unknown_distro_falls_back_to_generic_hint(tmp_path: Path) -> None:
    """An unrecognized ID yields the generic, manager-agnostic instruction."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run("voidlinux", str(empty), tmp_path)

    assert result.returncode == 1
    assert "your distribution package manager" in result.stderr
    assert "libvirt-dev" in result.stderr


def test_required_present_exits_zero(tmp_path: Path) -> None:
    """Stubbing uv + a permissive pkg-config satisfies the required tier."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("uv", "pkg-config"):
        stub = bindir / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    result = _run("debian", str(bindir), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Required dependencies missing" not in result.stderr
    assert "Required dependencies are present" in result.stdout
