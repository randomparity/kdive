# tests/scripts/test_check_local_libvirt.py
"""Behavioral tests for scripts/check-local-libvirt.sh.

Runtime state is faked via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override,
so the script's pass/fail paths run without a real libvirt host.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def _stub_python(bindir: Path, name: str, *, imports_ok: bool) -> Path:
    """Write a python-interpreter stub that succeeds (or fails) on `-c "import ..."`.

    Mirrors how the script probes the worker venv: `"$PY" -c "import guestfs, drgn"`.
    """
    body = "exit 0" if imports_ok else 'echo "ModuleNotFoundError" >&2\nexit 1'
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_all_healthy_exits_zero(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # virsh: any subcommand succeeds; `net-info default` reports Active: yes.
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=True)
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    staging = tmp_path / "install-staging"
    staging.mkdir()
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")  # readable; the ADR-0222 host-kernel probe passes
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(boot),
    }
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()


def test_unwritable_install_staging_fails_with_hint(tmp_path: Path) -> None:
    """A missing/unwritable install-staging dir fails with an actionable fix (boot-blocking)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=True)
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        # Points at a path that does not exist -> not a writable directory.
        "KDIVE_INSTALL_STAGING": str(tmp_path / "absent-staging"),
        # Absent boot dir -> the kernel probe skips, staying neutral for this assertion.
        "KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),
    }
    result = _run(env)
    assert result.returncode == 1
    assert "install staging" in result.stderr.lower()
    assert "KDIVE_INSTALL_STAGING" in result.stderr
    assert "$HOME" in result.stderr  # the hint must name the qemu-traversability trap


def test_missing_venv_bindings_fails_with_hint(tmp_path: Path) -> None:
    """The venv interpreter cannot import guestfs/drgn -> fail with an actionable fix."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=False)
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        "KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),
    }
    result = _run(env)
    assert result.returncode == 1
    err = result.stderr.lower()
    assert "guestfs" in err and "drgn" in err
    # The hint must point at both fixes: the live group and the libguestfs binding.
    assert "uv sync --group live" in result.stderr
    assert "python3-libguestfs" in result.stderr


def test_missing_kvm_node_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(tmp_path / "nope"),
        "KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),
    }
    result = _run(env)
    assert result.returncode == 1
    assert "kvm" in result.stderr.lower()


def test_user_not_in_libvirt_group_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm wheel")  # no 'libvirt'
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),
    }
    result = _run(env)
    assert result.returncode == 1
    assert "libvirt" in result.stderr.lower()


def _healthy_env(tmp_path: Path, bindir: Path, py: Path, boot: Path) -> dict[str, str]:
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    staging = tmp_path / "install-staging"
    staging.mkdir(exist_ok=True)
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(boot),
    }


def _healthy_bin(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=True)
    return bindir, py


def test_unreadable_host_kernel_fails_with_chmod_hint(tmp_path: Path) -> None:
    """An unreadable /boot/vmlinuz-* fails the preflight before the slow build (ADR-0222)."""
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()
    kernel = boot / "vmlinuz-6.8.0-124-generic"
    kernel.write_text("")
    # Strip all read bits so it is unreadable regardless of the (non-root) test UID.
    kernel.chmod(0o000)

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 1, result.stdout
    assert "vmlinuz" in result.stderr.lower()
    # The hint interpolates ${BOOT_DIR} (a tmp path under the test) and uses an arch-neutral
    # glob `vmlinu?-*` that matches both `vmlinuz-*` (x86_64) and `vmlinux-*` (ppc64le). Assert
    # on the semantic content — the fix command and the boot dir it targets — not the literal
    # `/boot/vmlinuz-*` string, which is neither what the script prints nor what a ppc64le
    # operator would need to type.
    assert f"chmod 0644 {boot}/vmlinu?-*" in result.stderr


def test_readable_host_kernel_passes(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.8.0-124-generic").write_text("")  # readable

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()


def test_absent_boot_kernels_skip_probe(tmp_path: Path) -> None:
    """No /boot/vmlinuz-* present (unusual layout) must skip, not fail on the literal glob."""
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()  # empty

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()


def _readable_boot(tmp_path: Path) -> Path:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")  # readable
    return boot


def test_nonroot_worker_on_system_uri_warns_advisory(tmp_path: Path) -> None:
    """A non-root worker under qemu:///system gets a non-failing advisory (ADR-0223): boot
    confirmation + host_dump cannot read root-owned virtlogd/QEMU output.
    Only fires when KDIVE_WORKER_AS_ROOT=0 (worker will not be sudo'd to root by up.sh)."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"  # pin non-root regardless of the CI runner's real uid
    env["KDIVE_WORKER_AS_ROOT"] = "0"  # explicitly opt out of root worker -> warning fires
    # KDIVE_LIBVIRT_URI unset -> defaults to qemu:///system

    result = _run(env)
    assert result.returncode == 0, result.stderr  # advisory, not a failure
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" in result.stderr.lower()
    assert "qemu:///session" in result.stderr  # the fix is named


def test_worker_as_root_default_suppresses_advisory(tmp_path: Path) -> None:
    """KDIVE_WORKER_AS_ROOT unset (default 1): up.sh will sudo the worker, so a non-root
    invoker sees no advisory — the worker identity is what matters, not the invoker's."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"  # non-root invoker
    # KDIVE_WORKER_AS_ROOT unset -> script defaults to 1 (the lib.sh/up.sh default)

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" not in result.stderr.lower()


def test_worker_as_root_explicit_1_suppresses_advisory(tmp_path: Path) -> None:
    """KDIVE_WORKER_AS_ROOT=1 explicitly set: advisory is suppressed for non-root invoker."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"
    env["KDIVE_WORKER_AS_ROOT"] = "1"

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" not in result.stderr.lower()


def test_session_uri_suppresses_advisory(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"
    env["KDIVE_LIBVIRT_URI"] = "qemu:///session"  # worker owns the QEMU process

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" not in result.stderr.lower()


def test_remote_transport_uri_suppresses_advisory(tmp_path: Path) -> None:
    """A transport-prefixed remote URI's root-owned files live on a different host, so the
    local-runner identity is irrelevant — no advisory."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"
    env["KDIVE_LIBVIRT_URI"] = "qemu+ssh://host/system"

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" not in result.stderr.lower()


def test_root_worker_on_system_uri_suppresses_advisory(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "0"  # a root worker reads root-owned files fine

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" not in result.stderr.lower()


def _bin_for_arch(
    tmp_path: Path, host_arch: str, qemu_binaries: tuple[str, ...]
) -> tuple[Path, Path]:
    """A healthy bindir with a stubbed ``uname -m`` and exactly the given qemu emulators present."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "uname", f"echo {host_arch}")
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-img", "exit 0")
    # A present qemu-system-ppc64 triggers the fadump version probe, which shells out to real
    # `sed`; expose it (only) so the probe runs, without leaking the host's other emulators.
    real_sed = shutil.which("sed")
    if real_sed is not None:
        (bindir / "sed").symlink_to(real_sed)
    for binary in qemu_binaries:
        _stub(bindir, binary, "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=True)
    return bindir, py


def test_ppc64le_host_does_not_require_x86_emulator(tmp_path: Path) -> None:
    """On a ppc64le host the native emulator is qemu-system-ppc64; a missing x86 one is no FAIL."""
    bindir, py = _bin_for_arch(tmp_path, "ppc64le", ("qemu-system-ppc64",))  # no x86 emulator
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert "qemu-system-x86_64 not found" not in result.stderr


def test_ppc64le_host_fails_for_missing_native_ppc_emulator(tmp_path: Path) -> None:
    """A ppc64le host lacking qemu-system-ppc64 fails, naming the ppc emulator (not the x86 one)."""
    bindir, py = _bin_for_arch(tmp_path, "ppc64le", ())  # no ppc emulator either
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))
    assert result.returncode == 1
    assert "qemu-system-ppc64 not found" in result.stderr
    assert "qemu-system-x86_64 not found" not in result.stderr


def test_x86_host_with_ppc_emulator_prints_tcg_advisory(tmp_path: Path) -> None:
    """With the foreign ppc emulator present on an x86 host, the TCG-only advisory prints."""
    bindir, py = _bin_for_arch(tmp_path, "x86_64", ("qemu-system-x86_64", "qemu-system-ppc64"))
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))
    out = result.stdout + result.stderr
    assert "guest arch ppc64le available via TCG only" in out
    assert "KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER" in out


def test_x86_host_without_ppc_emulator_prints_no_advisory(tmp_path: Path) -> None:
    """Absent foreign emulator → no advisory line (cross-arch is optional)."""
    bindir, py = _bin_for_arch(tmp_path, "x86_64", ("qemu-system-x86_64",))
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))
    assert "available via TCG only" not in (result.stdout + result.stderr)


def test_unsupported_host_arch_reports_unsupported_and_skips_native_qemu(tmp_path: Path) -> None:
    """An aarch64 host is told it is unsupported; no x86 fallback FAIL for a native emulator."""
    bindir, py = _bin_for_arch(tmp_path, "aarch64", ())
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))
    out = result.stdout + result.stderr
    assert "host arch aarch64 is not a supported kdive provisioning arch" in out
    assert "qemu-system-x86_64 not found" not in result.stderr
    assert "qemu-system-ppc64 not found" not in result.stderr
