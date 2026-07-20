"""Behavioral tests for scripts/check-setup-deps.sh.

The script's value is its per-distro install hints, so these tests drive it with a
synthetic os-release (via KDIVE_OS_RELEASE) and a controlled PATH, then assert the
package names and exit status for each distro family. The script uses only shell
builtins, so an empty PATH makes every external dependency look missing without
needing stubs.
"""

from __future__ import annotations

import os
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

# run_privileged only escalates via sudo when EUID != 0; under a root pytest (some CI
# containers) sudo is skipped and the sudo-log assertions have no file to read.
skip_if_root = pytest.mark.skipif(os.geteuid() == 0, reason="sudo path only runs as non-root")


def _run(
    os_release_id: str,
    path: str,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the checker with a forced distro and a controlled PATH."""
    assert BASH is not None, "bash is required to run the checker"
    os_release = tmp_path / "os-release"
    os_release.write_text(f"ID={os_release_id}\n")
    env = {
        "PATH": path,
        "KDIVE_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path),
        **(extra_env or {}),
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *(args or [])],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _sudo_stub(bindir: Path, log: Path, *, preflight_ok: bool = True) -> None:
    """A sudo stub that logs its argv, strips leading -n/-v, then dispatches.

    Production calls `sudo -n <cmd>` / `sudo -n true` / `sudo -v`; a naive `exec "$@"` would run
    `exec -n …` (invalid option), so the stub must strip the flag first.
    """
    fail = "exit 1\n" if not preflight_ok else ""
    body = (
        "#!/bin/sh\n"
        f'echo "sudo $@" >> "{log}"\n'
        f"{fail}"
        'while [ "$1" = -n ] || [ "$1" = -v ]; do shift; done\n'
        "[ $# -eq 0 ] && exit 0\n"  # `sudo -v` (now empty) = credential preflight OK
        '[ "$1" = true ] && exit 0\n'  # `sudo -n true` preflight OK
        'exec "$@"\n'
    )
    _stub(bindir, "sudo", body)


def _coreutils_dir(tmp_path: Path) -> Path:
    """Expose only ln/chmod/touch (via Python symlinks) for fix-effect tests, appended AFTER the
    stub bindir so stubs still shadow and genuinely-absent deps (pkg-config) stay absent."""
    d = tmp_path / "coreutils"
    d.mkdir()
    for name in ("ln", "chmod", "touch"):
        target = shutil.which(name)
        assert target is not None, f"{name} is required for the fix-effect tests"
        os.symlink(target, d / name)
    return d


def _bin(tmp_path: Path) -> Path:
    """A bindir with uv + pkg-config present so the Required tier is otherwise satisfiable."""
    b = tmp_path / "bin"
    b.mkdir()
    _stub(b, "uv", "#!/bin/sh\nexit 0\n")
    _stub(b, "pkg-config", "#!/bin/sh\nexit 0\n")
    return b


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
    distro_id: str,
    host_arch: str,
    present: tuple[str, ...],
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
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
    return _run(distro_id, str(bindir), tmp_path, extra_env=extra_env)


def test_advisory_shows_host_arch_first(tmp_path: Path) -> None:
    """The advisory names the host arch first, then guest arches (native/host before foreign)."""
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    result = _run_with_uname(
        "debian",
        "x86_64",
        ("qemu-system-x86_64", "qemu-system-ppc64"),
        tmp_path,
        extra_env={"KDIVE_KVM_NODE": str(kvm)},
    )
    out = result.stdout
    assert "Host architecture: x86_64 (supported kdive provisioning arch)" in out
    assert out.index("guest arch x86_64:") < out.index("guest arch ppc64le:")
    native = "guest arch x86_64: available natively via qemu-system-x86_64 (/dev/kvm accessible"
    assert native in out
    assert "guest arch ppc64le: available via TCG only (qemu-system-ppc64)" in out


def test_advisory_native_line_when_kvm_absent(tmp_path: Path) -> None:
    """Native emulator present but /dev/kvm inaccessible → the TCG-fallback native line."""
    result = _run_with_uname(
        "debian",
        "x86_64",
        ("qemu-system-x86_64",),
        tmp_path,
        extra_env={"KDIVE_KVM_NODE": str(tmp_path / "nokvm")},
    )
    assert "guest arch x86_64: native emulator present, /dev/kvm not accessible" in result.stdout


def test_advisory_native_line_when_qemu_absent(tmp_path: Path) -> None:
    """No native emulator → name the package to install for native guests."""
    result = _run_with_uname("debian", "x86_64", (), tmp_path)
    want = "guest arch x86_64: not available; install qemu-system-x86 for native guests"
    assert want in result.stdout


# ── Task 2: opt-in package auto-install ──────────────────────────────────────


def test_non_tty_without_yes_stays_report_only(tmp_path: Path) -> None:
    """No -y and piped stdin => no install/sudo command ever runs (report-only contract)."""
    b = _bin(tmp_path)
    log = tmp_path / "cmd.log"
    _stub(b, "apt-get", f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0')
    _stub(b, "sudo", f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0')
    _run("debian", str(b), tmp_path)  # missing recommended/future deps, but no fix offered
    assert not log.exists()


@skip_if_root
def test_yes_installs_with_refresh_and_noninteractive_flag_and_sudo_n(tmp_path: Path) -> None:
    b = _bin(tmp_path)
    log = tmp_path / "cmd.log"
    sudolog = tmp_path / "sudo.log"
    _stub(b, "apt-get", f'#!/bin/sh\necho "apt-get $@" >> "{log}"\nexit 0')
    _sudo_stub(b, sudolog)
    _run("debian", str(b), tmp_path, args=["-y"])
    logged = log.read_text()
    assert "apt-get update" in logged
    assert "apt-get install -y" in logged
    assert "sudo -n" in sudolog.read_text()  # non-root path uses sudo -n under -y


@skip_if_root
def test_yes_sudo_preflight_failure_skips_with_message_no_hang(tmp_path: Path) -> None:
    b = _bin(tmp_path)
    log = tmp_path / "cmd.log"
    _stub(b, "apt-get", f'#!/bin/sh\necho installed >> "{log}"\nexit 0')
    _sudo_stub(b, tmp_path / "sudo.log", preflight_ok=False)  # sudo -n true fails (no NOPASSWD)
    r = _run("debian", str(b), tmp_path, args=["-y"])
    assert "passwordless sudo" in r.stderr
    assert not log.exists()  # install never attempted


@skip_if_root
def test_yes_install_failure_reported_not_fatal(tmp_path: Path) -> None:
    b = _bin(tmp_path)
    _stub(b, "apt-get", "#!/bin/sh\nexit 100")
    _sudo_stub(b, tmp_path / "sudo.log")
    r = _run("debian", str(b), tmp_path, args=["-y"])
    assert "failed to install" in r.stderr  # reported
    # script did not abort mid-run: the advisory still printed. _bin stubs no `uname`, so
    # host_arch is empty and the advisory takes its unsupported-host branch.
    assert "not a supported kdive provisioning arch" in r.stdout


@skip_if_root
def test_manual_hint_tools_not_auto_installed_under_yes(tmp_path: Path) -> None:
    b = _bin(tmp_path)
    log = tmp_path / "curl.log"
    _stub(b, "curl", f'#!/bin/sh\necho ran >> "{log}"\nexit 0')
    _sudo_stub(b, tmp_path / "sudo.log")
    _stub(b, "apt-get", "#!/bin/sh\nexit 0")
    _run("debian", str(b), tmp_path, args=["-y"])
    assert not log.exists()  # uv/rustup/just/prek curl|sh never executed


@skip_if_root
def test_reverify_after_install_exits_zero(tmp_path: Path) -> None:
    """A required item missing at start, materialized by the install stub, is found on re-probe."""
    b = tmp_path / "bin"
    b.mkdir()
    _stub(b, "uv", "#!/bin/sh\nexit 0\n")  # required manual-hint tool present
    _sudo_stub(b, tmp_path / "sudo.log")
    # pkg-config MISSING initially (so Required is unsatisfied); the install "creates" it,
    # and the new pkg-config exits 0 so the header probes pass too.
    apt = (
        "#!/bin/sh\n"
        f'printf "#!/bin/sh\\nexit 0\\n" > "{b}/pkg-config"\n'
        f'chmod 0755 "{b}/pkg-config"\n'
        "exit 0\n"
    )
    _stub(b, "apt-get", apt)
    path = f"{b}:{_coreutils_dir(tmp_path)}"  # chmod available for the install stub
    r = _run("debian", path, tmp_path, args=["-y"])
    assert r.returncode == 0, r.stderr
    assert "re-checking after fixes" in r.stderr
    recheck = r.stderr.split("re-checking after fixes")[1]
    assert "Required dependencies missing" not in recheck


@skip_if_root
def test_interactive_accept_uses_plain_sudo(tmp_path: Path) -> None:
    """A TTY operator who answers 'y' gets plain sudo (password allowed), not sudo -n."""
    import pty

    b = _bin(tmp_path)
    log = tmp_path / "cmd.log"
    sudolog = tmp_path / "sudo.log"
    _stub(b, "apt-get", f'#!/bin/sh\necho installed >> "{log}"\nexit 0')
    _sudo_stub(b, sudolog)
    # guestfs importable → no guestfs prompt, so the prompt count is deterministic
    venv_py = _stub_python(b, "venv-python", imports_ok=True)
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\n")
    env = {
        "PATH": str(b),
        "KDIVE_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path),
        "KDIVE_PYTHON": str(venv_py),
    }
    assert BASH is not None
    controller, worker = pty.openpty()
    os.write(controller, b"y\n" * 6)  # generously more y's than prompts; extras are harmless
    proc = subprocess.Popen(
        [BASH, str(SCRIPT)],
        stdin=worker,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    os.close(worker)
    proc.communicate(timeout=30)  # drains pipes concurrently (no deadlock)
    os.close(controller)
    logged = sudolog.read_text()
    assert "sudo -v" in logged  # interactive credential preflight is plain sudo -v
    assert "sudo -n" not in logged  # never the non-interactive flavor at a TTY
    assert "installed" in log.read_text()


def test_report_only_output_unchanged(tmp_path: Path) -> None:
    """A non-TTY, no-arg run does not enter the fix path (no re-check, no double report)."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run("debian", str(empty), tmp_path)
    assert "re-checking after fixes" not in result.stderr
    # the 'missing' report appears exactly once per tier (no double render)
    assert result.stderr.count("Required dependencies missing") == 1


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


def _stub_python(bindir: Path, name: str, *, imports_ok: bool) -> Path:
    """Write a python-interpreter stub that succeeds (or fails) on ``-c "import ..."``.

    Mirrors how the checker probes the worker venv: ``"$PY" -c "import guestfs"``.
    """
    body = "exit 0" if imports_ok else 'echo "ModuleNotFoundError" >&2\nexit 1'
    py = bindir / name
    py.write_text(f"#!/bin/sh\n{body}\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return py


def test_pyimport_probes_venv_interpreter_not_system_python3(tmp_path: Path) -> None:
    """The libguestfs probe uses the venv interpreter (KDIVE_PYTHON), not system python3 (#1328).

    System python3 can import guestfs, but the worker's venv cannot — the check must report
    the missing binding rather than trust system python3 for a false green.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "pkg-config", "#!/bin/sh\nexit 0\n")
    _stub_python(bindir, "python3", imports_ok=True)  # system python3 can import guestfs
    venv_py = _stub_python(bindir, "venv-python", imports_ok=False)  # the venv cannot

    result = _run("debian", str(bindir), tmp_path, extra_env={"KDIVE_PYTHON": str(venv_py)})

    assert "python3-guestfs" in result.stderr, result.stderr


def test_pyimport_trusts_venv_over_system_python3_when_venv_has_binding(tmp_path: Path) -> None:
    """A working venv binding suppresses the guestfs hint even when system python3 lacks it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "pkg-config", "#!/bin/sh\nexit 0\n")
    _stub_python(bindir, "python3", imports_ok=False)  # system python3 cannot import guestfs
    venv_py = _stub_python(bindir, "venv-python", imports_ok=True)  # but the venv can

    result = _run("debian", str(bindir), tmp_path, extra_env={"KDIVE_PYTHON": str(venv_py)})

    assert "python3-guestfs" not in result.stderr, result.stderr


def test_guestfs_hint_names_the_venv_symlink_remedy(tmp_path: Path) -> None:
    """A venv that cannot import guestfs is hinted to symlink the binding into the venv, not just
    to install the package — a uv venv has no system-site-packages, so an already-installed
    package is a dead-end fix (#1328). The hint must point at the runbook, mirroring the check.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "pkg-config", "#!/bin/sh\nexit 0\n")
    venv_py = _stub_python(bindir, "venv-python", imports_ok=False)

    result = _run("debian", str(bindir), tmp_path, extra_env={"KDIVE_PYTHON": str(venv_py)})

    assert "symlink" in result.stderr, result.stderr
    assert "four-method-live-run.md" in result.stderr, result.stderr


def test_autodetects_repo_venv_under_relative_invocation(tmp_path: Path) -> None:
    """With KDIVE_PYTHON unset, the guestfs probe autodetects the repo .venv even when the script
    is invoked by a relative path (`bash scripts/check-setup-deps.sh` from the repo root, #1328).

    The venv interpreter can import guestfs; system python3 cannot. A relative invocation must
    still resolve the venv (anchored to $PWD), so the guestfs hint stays suppressed.
    """
    assert BASH is not None
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / "check-setup-deps.sh")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _stub_python(venv_bin, "python", imports_ok=True)  # the repo venv can import guestfs
    bindir = repo / "bin"
    bindir.mkdir()
    _stub(bindir, "uv", "#!/bin/sh\nexit 0\n")
    _stub(bindir, "pkg-config", "#!/bin/sh\nexit 0\n")
    _stub_python(bindir, "python3", imports_ok=False)  # system python3 cannot -> emits the hint
    os_release = repo / "os-release"
    os_release.write_text("ID=debian\n")

    result = subprocess.run(
        [BASH, "scripts/check-setup-deps.sh"],  # relative path; KDIVE_PYTHON unset
        cwd=str(repo),
        env={"PATH": str(bindir), "KDIVE_OS_RELEASE": str(os_release), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert "python3-guestfs" not in result.stderr, result.stderr
