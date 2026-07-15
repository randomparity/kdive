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


def _stub(bindir: Path, name: str, body: str) -> None:
    stub = bindir / name
    stub.write_text(body)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_ppc64le_future_hint_names_the_power_qemu_package(tmp_path: Path) -> None:
    """On a ppc64le host the future tier asks for qemu-system-ppc, not the x86 emulator.

    The QEMU binary name is arch-derived (ppc64le -> qemu-system-ppc64), so a stubbed
    ``uname -m`` reporting ppc64le must route the debian install hint to qemu-system-ppc.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "pkg-config", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "uname", "#!/bin/sh\necho ppc64le\n")

    result = _run("debian", str(bindir), tmp_path)

    assert "qemu-system-ppc" in result.stderr, result.stderr
    assert "qemu-system-x86" not in result.stderr


def _run_with_uname(
    distro_id: str, host_arch: str, present: tuple[str, ...], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run the checker with a stubbed ``uname -m`` and a controlled set of present binaries.

    ``uv`` + ``pkg-config`` are always stubbed present so the required tier is satisfied and the
    cross-arch advisory (which runs regardless) is reached on a clean exit.
    """
    # A unique bindir per call so a test may invoke the checker more than once (e.g. the
    # symmetric present/absent pair) without the second mkdir colliding on the first.
    bindir = tmp_path / f"bin-{host_arch}-{'-'.join(present) or 'none'}"
    bindir.mkdir()
    _stub(bindir, "uname", f"#!/bin/sh\necho {host_arch}\n")
    for tool in ("uv", "pkg-config", *present):
        _stub(bindir, tool, "#!/bin/sh\nexit 0\n")
    return _run(distro_id, str(bindir), tmp_path)


def test_cross_arch_advisory_names_foreign_package_when_absent_on_x86(tmp_path: Path) -> None:
    """On x86_64 with no ppc64 emulator, the advisory names the exact ppc64 package (stdout)."""
    result = _run_with_uname("debian", "x86_64", (), tmp_path)
    assert "guest arch ppc64le: not available; install qemu-system-ppc" in result.stdout
    # An informational advisory must not leak into the missing-dependency (stderr) channel.
    assert "guest arch ppc64le" not in result.stderr


def test_cross_arch_advisory_reports_tcg_available_when_foreign_qemu_present(
    tmp_path: Path,
) -> None:
    """With the foreign qemu present, the advisory says 'available via TCG only', not install."""
    result = _run_with_uname("debian", "x86_64", ("qemu-system-ppc64",), tmp_path)
    assert "guest arch ppc64le: available via TCG only (qemu-system-ppc64)" in result.stdout
    assert "install qemu-system-ppc" not in result.stdout


def test_cross_arch_advisory_is_symmetric_on_ppc64le_host(tmp_path: Path) -> None:
    """On a ppc64le host the foreign arch is x86_64; absent → name the x86 package (debian)."""
    absent = _run_with_uname("debian", "ppc64le", (), tmp_path)
    assert "guest arch x86_64: not available; install qemu-system-x86" in absent.stdout
    present = _run_with_uname("debian", "ppc64le", ("qemu-system-x86_64",), tmp_path)
    assert "guest arch x86_64: available via TCG only (qemu-system-x86_64)" in present.stdout


def test_cross_arch_advisory_uses_opensuse_package_names(tmp_path: Path) -> None:
    """openSUSE splits the packages differently (qemu-ppc), matching package_for."""
    result = _run_with_uname("opensuse", "x86_64", (), tmp_path)
    assert "guest arch ppc64le: not available; install qemu-ppc" in result.stdout


def test_unsupported_host_arch_skips_native_qemu_and_advisory(tmp_path: Path) -> None:
    """An aarch64 host is told it is unsupported; no x86 fallback, no cross-arch advisory."""
    result = _run_with_uname("debian", "aarch64", (), tmp_path)
    assert "host arch aarch64 is not a supported kdive provisioning arch" in result.stdout
    assert "guest arch" not in result.stdout  # no cross-arch advisory
    # The future tier must not demand a native qemu for an unsupported host arch.
    assert "qemu-system-x86" not in result.stderr
    assert "qemu-system-ppc" not in result.stderr


def test_ppc64le_missing_rust_fails_with_rustup_hint(tmp_path: Path) -> None:
    """On a wheel-less ppc64le host with no Rust toolchain, the required tier fails with rustup.

    uv + pkg-config are stubbed present, so the only missing required item is the Rust
    toolchain; the check must exit 1 and route the fix through the manual rustup hint (not a
    distro package-manager line).
    """
    result = _run_with_uname("debian", "ppc64le", (), tmp_path)

    assert result.returncode == 1, result.stdout
    assert "Required dependencies missing" in result.stderr
    # rustup is a manual hint (not a distro package line): label + rustup command together.
    assert "Tooling not provided by your distribution" in result.stderr
    assert "rustc/cargo: curl" in result.stderr
    assert "sh.rustup.rs" in result.stderr
    # It must not be routed to a distro package-manager install line.
    assert "install rustc" not in result.stderr
    assert "install cargo" not in result.stderr


def test_ppc64le_with_rust_present_does_not_flag_rust(tmp_path: Path) -> None:
    """With rustc + cargo on PATH, the ppc64le required tier no longer names a Rust toolchain."""
    result = _run_with_uname("debian", "ppc64le", ("rustc", "cargo"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Required dependencies missing" not in result.stderr
    assert "sh.rustup.rs" not in result.stderr
    assert "rustc/cargo" not in result.stderr


def test_x86_64_never_requires_rust(tmp_path: Path) -> None:
    """x86_64 has prebuilt wheels, so a missing Rust toolchain raises no requirement (unchanged)."""
    result = _run_with_uname("debian", "x86_64", (), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "sh.rustup.rs" not in result.stdout + result.stderr
    assert "rustc/cargo" not in result.stdout + result.stderr


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
