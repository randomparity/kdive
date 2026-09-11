"""Gate tests for examples/local-libvirt/install-host.sh.

The script prepares a whole host, so these tests drive only the gates it evaluates *before* its
first privileged action: the distro-family gate, the arch/emulator gate, and the Enterprise Linux
container-engine gate. Each runs with a synthetic os-release (KDIVE_OS_RELEASE) and a controlled
PATH of stubs, so nothing is ever installed. A host that clears every gate reaches the `sudo`
credential preflight, and the stub there records that it was reached and stops the script — which
is what the admit-side tests assert, rather than an exit status shared with unrelated failures.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "install-host.sh"
BASH = shutil.which("bash")

# The script uses `read -r a b < <(...)` process substitution and `((...))` arithmetic.
pytestmark = requires_bash(4, 3, "process substitution in the kernel-permission loop")

_SUDO_REACHED = "sudo-reached"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


def _bindir(tmp_path: Path, *, arch: str = "x86_64", docker: bool = False) -> Path:
    """A PATH holding just enough for the gates: uname, a marker-writing sudo, optional docker."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    # The script resolves its own directory through `dirname` before any gate runs, so the real
    # one has to be reachable; everything else on this PATH is a stub.
    real_dirname = shutil.which("dirname")
    assert real_dirname is not None, "dirname is required to resolve the script's own directory"
    (b / "dirname").symlink_to(real_dirname)
    _stub(b, "uname", f"#!/bin/sh\necho {arch}\n")
    # The gates all sit above `sudo -n true || sudo -v`. Recording the call and failing stops the
    # run at a known line under `set -e`, so an admitted host is proven by the marker, not by an
    # exit status that a later unrelated failure could also produce. The marker is written with a
    # redirect rather than `touch`: the stub inherits this same PATH and has no coreutils on it.
    _stub(b, "sudo", f'#!/bin/sh\n: > "{tmp_path / _SUDO_REACHED}"\nexit 1\n')
    if docker:
        _stub(b, "docker", "#!/bin/sh\nexit 0\n")
    return b


def _run(
    tmp_path: Path,
    *,
    os_release: str,
    arch: str = "x86_64",
    docker: bool = False,
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None, "bash is required to run the installer"
    release_file = tmp_path / "os-release"
    release_file.write_text(os_release)
    bindir = _bindir(tmp_path, arch=arch, docker=docker)
    return subprocess.run(
        [BASH, str(SCRIPT)],
        env={
            "PATH": str(bindir),
            "KDIVE_OS_RELEASE": str(release_file),
            "HOME": str(tmp_path),
            "USER": "tester",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _reached_sudo(tmp_path: Path) -> bool:
    return (tmp_path / _SUDO_REACHED).exists()


def test_unsupported_distro_family_is_refused(tmp_path: Path) -> None:
    """Arch Linux matches no family token, so the script must stop before any host change."""
    result = _run(tmp_path, os_release="ID=arch\n")

    assert result.returncode == 2
    assert "Debian/Ubuntu (apt) and Fedora/RHEL-family (dnf)" in result.stderr
    assert "docs/operating/providers/local-libvirt.md" in result.stderr
    assert not _reached_sudo(tmp_path), "an unsupported host must not reach the sudo preflight"


@pytest.mark.parametrize(
    ("label", "os_release"),
    [
        ("ubuntu", "ID=ubuntu\nID_LIKE=debian\n"),
        ("debian", "ID=debian\n"),
        ("fedora", "ID=fedora\n"),
        ("rhel", 'ID=rhel\nID_LIKE="fedora"\n'),
        ("centos", 'ID=centos\nID_LIKE="rhel fedora"\n'),
        ("rocky", 'ID=rocky\nID_LIKE="rhel centos fedora"\n'),
        # The three RedHat tokens are matched independently, so each needs a case that carries
        # only that token. Without these the `fedora` token alone would satisfy every RedHat row
        # above — the rhel and centos arms could be deleted and the suite would stay green.
        ("rhel-no-id-like", "ID=rhel\n"),
        ("centos-no-id-like", "ID=centos\n"),
        ("rhel-like-only", 'ID=ol\nID_LIKE="rhel"\n'),
    ],
)
def test_supported_families_clear_every_gate(label: str, os_release: str, tmp_path: Path) -> None:
    """Both supported families, including EL rebuilds reached through ID_LIKE, are admitted.

    EL gets a docker stub because its container-engine gate is asserted separately below.
    """
    result = _run(tmp_path, os_release=os_release, docker=True)

    assert "covers Debian/Ubuntu" not in result.stderr, f"{label} was refused: {result.stderr}"
    assert _reached_sudo(tmp_path), f"{label} did not reach the sudo preflight: {result.stderr}"


def test_unsupported_arch_is_refused(tmp_path: Path) -> None:
    """s390x has no emulator package mapping in either family."""
    result = _run(tmp_path, os_release="ID=fedora\n", arch="s390x")

    assert result.returncode == 2
    assert "s390x is not a supported kdive provisioning arch" in result.stderr
    assert not _reached_sudo(tmp_path)


def test_enterprise_linux_without_a_container_engine_is_refused(tmp_path: Path) -> None:
    """EL packages no engine this script can install, so it must say so before installing anything.

    The message has to carry both remedies; an EL operator cannot act on the refusal otherwise.
    """
    result = _run(
        tmp_path,
        os_release='ID=rocky\nID_LIKE="rhel centos fedora"\n',
        docker=False,
    )

    assert result.returncode == 2
    assert "Enterprise Linux packages no container engine" in result.stderr
    assert "docker-ce" in result.stderr
    assert "podman-docker" in result.stderr
    assert not _reached_sudo(tmp_path), "the EL refusal must precede the sudo preflight"


def test_fedora_without_docker_is_not_refused(tmp_path: Path) -> None:
    """Fedora installs moby-engine itself, so the EL container gate must not catch it."""
    result = _run(tmp_path, os_release="ID=fedora\n", docker=False)

    assert "Enterprise Linux packages no container engine" not in result.stderr
    assert _reached_sudo(tmp_path)
