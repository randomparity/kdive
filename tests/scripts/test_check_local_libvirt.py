# tests/scripts/test_check_local_libvirt.py
"""Behavioral tests for scripts/operations/check-local-libvirt.sh.

Runtime state is faked via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override,
so the script's pass/fail paths run without a real libvirt host.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "operations" / "check-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    # Pin the off-PATH emulator location to an absent path. Its default is the real
    # /usr/libexec/qemu-kvm, which qemu-kvm-core installs on the whole RHEL family — leaving it
    # unset would make these tests read the host and fail on exactly the distros this change
    # exists to support. A caller that wants it present passes its own KDIVE_QEMU_LIBEXEC.
    full_env = {"KDIVE_QEMU_LIBEXEC": "/nonexistent/qemu-kvm", **env}
    return subprocess.run(
        [BASH, str(SCRIPT)], env=full_env, capture_output=True, text=True, check=False
    )


def _stub_python(bindir: Path, name: str, *, imports_ok: bool) -> Path:
    """Write a python-interpreter stub that succeeds (or fails) on `-c "import ..."`.

    Mirrors how the script probes KDIVE_PYTHON, the checkout/CLI interpreter:
    `"$PY" -c "import guestfs, drgn"`.
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


def test_autodetects_repo_venv_under_relative_invocation(tmp_path: Path) -> None:
    """With KDIVE_PYTHON unset, the guestfs/drgn probe autodetects the repo .venv even under a
    relative invocation (running the operations script from the repo root, #1328).

    The planted repo venv can import guestfs+drgn; system python3 on PATH cannot. A relative
    invocation must still resolve the venv ($PWD-anchored), so the probe reports OK, not fail.
    """
    assert BASH is not None
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    operations = scripts / "operations"
    operations.mkdir(parents=True)
    shutil.copy(SCRIPT, operations / "check-local-libvirt.sh")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _stub_python(venv_bin, "python", imports_ok=True)  # the repo venv can import guestfs, drgn
    bindir = repo / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    _stub_python(bindir, "python3", imports_ok=False)  # system python3 lacks the bindings
    kvm = repo / "kvm"
    kvm.write_text("")
    staging = repo / "install-staging"
    staging.mkdir()
    boot = repo / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")

    result = subprocess.run(
        [BASH, "scripts/operations/check-local-libvirt.sh"],  # relative path; KDIVE_PYTHON unset
        cwd=str(repo),
        env={
            "PATH": str(bindir),
            "HOME": str(tmp_path),
            "KDIVE_KVM_NODE": str(kvm),
            "KDIVE_INSTALL_STAGING": str(staging),
            "KDIVE_BOOT_DIR": str(boot),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    # The venv (not the failing system python3) satisfied the guestfs+drgn probe: OK, exit 0.
    assert result.returncode == 0, result.stderr
    assert "imports guestfs and drgn" in result.stderr


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
    # The old "section 4b" pointer named a heading that no longer exists in that runbook.
    assert "Wire the worker venv" in result.stderr, result.stderr
    assert "section 4b" not in result.stderr, result.stderr
    # The failing check names the interpreter it actually probes (KDIVE_PYTHON, the
    # checkout/CLI interpreter) rather than calling it the "worker venv" -- this checkout
    # interpreter is not the running lifecycle worker (issue #2759).
    fail_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("FAIL") and "guestfs, drgn" in line
    )
    assert "KDIVE_PYTHON" in fail_line, fail_line
    assert "worker" not in fail_line.lower(), fail_line


def test_missing_venv_bindings_optional_warns(tmp_path: Path) -> None:
    """KDIVE_PREFLIGHT_KDUMP=optional downgrades only the guestfs/drgn probe to a WARN.

    The same fix text is still printed, and the host is reported ready (exit 0) because every
    other check passes.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=False)
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    staging = tmp_path / "install-staging"
    staging.mkdir()
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),
        "KDIVE_PREFLIGHT_KDUMP": "optional",
    }
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert "FAIL" not in result.stderr
    assert "guestfs" in result.stderr and "drgn" in result.stderr
    assert "python3-libguestfs" in result.stderr
    assert "host is ready" in result.stderr
    # The WARN path's message went through the same rename as the FAIL path: it must name
    # KDIVE_PYTHON, not the "worker venv" (issue #2759).
    warn_line = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith("WARN") and "guestfs, drgn" in line
    )
    assert "KDIVE_PYTHON" in warn_line, warn_line
    assert "worker" not in warn_line.lower(), warn_line


def test_invalid_kdump_preflight_value_rejected(tmp_path: Path) -> None:
    """A typo in KDIVE_PREFLIGHT_KDUMP is an error, not a silent fallback to 'required'."""
    env = {
        "PATH": str(tmp_path),
        "HOME": str(tmp_path),
        "KDIVE_PREFLIGHT_KDUMP": "optinal",
    }
    result = _run(env)
    assert result.returncode == 2
    assert "KDIVE_PREFLIGHT_KDUMP=optinal is not valid" in result.stderr


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


def test_published_session_checks_its_uri_without_system_prerequisites(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    uri = "qemu+unix:///session?socket=/tmp/libvirt-sock"
    _stub(bindir, "id", "echo kvm")  # hosted runner has no libvirt group
    _stub(bindir, "virsh", f'[ "$*" = "-c {uri} list" ]')
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_LIBVIRT_URI"] = uri

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "libvirt' group" not in result.stderr
    assert "default' network" not in result.stderr


def test_unreachable_configured_session_fails_even_if_system_works(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    _stub(bindir, "virsh", 'case "$*" in *qemu:///system*) echo "Active: yes";; *) exit 1;; esac')
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_LIBVIRT_URI"] = "qemu:///session"

    result = _run(env)
    assert result.returncode == 1
    assert "cannot connect" in result.stderr
    assert "KDIVE_LIBVIRT_URI" in result.stderr
    assert "qemu:///session" not in result.stderr


def test_default_system_network_must_be_active(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: no";; esac\nexit 0')
    result = _run(_healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path)))

    assert result.returncode == 1
    assert "default' network is not active" in result.stderr


def test_explicit_local_system_socket_checks_its_network(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    uri = "qemu+unix:///system?socket=/tmp/system-libvirt-sock"
    _stub(
        bindir,
        "virsh",
        f'case "$*" in "-c {uri} list") exit 0;; '
        f'"-c {uri} net-info default") echo "Active: yes";; *) exit 1;; esac',
    )
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_LIBVIRT_URI"] = uri

    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "default' network is active" in result.stderr


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
    # 0640 root:kvm, matching what `just prepare-local-libvirt-host` declares — a 0644 remedy
    # here would hand the operator a wider mode than provisioning establishes (#2479). The recipe
    # installs the durable hook; only the fallback needs to be reapplied after a kernel upgrade.
    assert "just prepare-local-libvirt-host" in result.stderr
    assert "installs the durable post-upgrade hook" in result.stderr
    assert f"sudo chgrp kvm {boot}/vmlinu?-* && sudo chmod 0640 {boot}/vmlinu?-*" in result.stderr
    assert "chmod 0644" not in result.stderr
    assert "dpkg-statoverride" not in result.stderr


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
    confirmation + host_dump cannot read root-owned virtlogd/QEMU output."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    env["KDIVE_EFFECTIVE_UID"] = "1000"  # pin non-root regardless of the CI runner's real uid
    # KDIVE_LIBVIRT_URI unset -> defaults to qemu:///system

    result = _run(env)
    assert result.returncode == 0, result.stderr  # advisory, not a failure
    assert "ready" in result.stderr.lower()
    assert "boot-confirmation" in result.stderr.lower()
    assert "qemu:///session" in result.stderr  # the fix is named


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


def test_unlistable_boot_dir_fails_the_kernel_probe(tmp_path: Path) -> None:
    """An unlistable /boot must fail, not skip: the glob finds nothing, and the `found=0`
    skip would otherwise report OK on the exact host state the probe exists to catch (#2479).
    check-setup-deps.sh's probe_boot_kernels carries the same guard."""
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.8.0-124-generic").write_text("")
    boot.chmod(0o000)
    try:
        result = _run(_healthy_env(tmp_path, bindir, py, boot))
    finally:
        boot.chmod(0o755)  # restore so pytest can clean the tmp tree up

    assert result.returncode == 1, result.stdout
    assert "is not readable by this user" in result.stderr, result.stderr


def _selinux_env(
    tmp_path: Path, *, mode: str, rootfs_type: str, install_type: str
) -> dict[str, str]:
    """A healthy host whose getenforce reports ``mode`` and whose ``stat -c %C`` reports each
    image directory's SELinux type (ADR-0640, #2779)."""
    bindir, py = _healthy_bin(tmp_path)
    env = _healthy_env(tmp_path, bindir, py, _readable_boot(tmp_path))
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    env["KDIVE_ROOTFS_DIR"] = str(rootfs)
    _stub(bindir, "getenforce", f"echo {mode}")
    _stub(
        bindir,
        "stat",
        'case "$*" in\n'
        f'  *" {rootfs}") echo system_u:object_r:{rootfs_type}:s0 ;;\n'
        f'  *" {env["KDIVE_INSTALL_STAGING"]}") echo system_u:object_r:{install_type}:s0 ;;\n'
        "  *) exit 1 ;;\n"
        "esac",
    )
    return env


def test_enforcing_host_with_svirt_image_labels_passes(tmp_path: Path) -> None:
    env = _selinux_env(
        tmp_path, mode="Enforcing", rootfs_type="svirt_image_t", install_type="svirt_image_t"
    )
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("is labeled svirt_image_t") == 2, result.stderr


@pytest.mark.parametrize("mislabeled", ["rootfs", "install"])
def test_enforcing_host_without_svirt_image_label_fails(tmp_path: Path, mislabeled: str) -> None:
    """A host prepared without the ADR-0640 rules fails the check, naming the directory (#2779)."""
    types = {"rootfs": "svirt_image_t", "install": "svirt_image_t", mislabeled: "var_lib_t"}
    env = _selinux_env(
        tmp_path, mode="Enforcing", rootfs_type=types["rootfs"], install_type=types["install"]
    )
    result = _run(env)
    directory = env["KDIVE_ROOTFS_DIR" if mislabeled == "rootfs" else "KDIVE_INSTALL_STAGING"]
    assert result.returncode == 1, result.stderr
    assert f"FAIL  {directory} is labeled var_lib_t, not svirt_image_t" in result.stderr
    assert "just prepare-local-libvirt-host" in result.stderr
    assert f"kdive_label_svirt_image {directory}" in result.stderr


def test_unreadable_label_on_enforcing_host_fails(tmp_path: Path) -> None:
    env = _selinux_env(
        tmp_path, mode="Enforcing", rootfs_type="svirt_image_t", install_type="svirt_image_t"
    )
    env["KDIVE_ROOTFS_DIR"] = str(tmp_path / "absent-rootfs")
    result = _run(env)
    assert result.returncode == 1, result.stderr
    assert "absent-rootfs is labeled unknown, not svirt_image_t" in result.stderr


@pytest.mark.parametrize("mode", ["Permissive", "Disabled"])
def test_non_enforcing_host_skips_the_label_check(tmp_path: Path, mode: str) -> None:
    env = _selinux_env(tmp_path, mode=mode, rootfs_type="var_lib_t", install_type="var_lib_t")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "svirt_image_t" not in result.stderr
