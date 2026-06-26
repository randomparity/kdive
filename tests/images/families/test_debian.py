"""Unit tests for the debian FamilyCustomizer argv contract (ADR-0251, #824).

These pin the virt-customize argv the debian customizer builds without running libguestfs: apt
install, ``ssh.service``/``kdump-tools.service`` enable, ``USE_KDUMP=1``, the NMI-panic sysctl,
ssh-inject, the kdive-ready unit, and the version-proof cloud-init disable + machine-id seed on a
cloud-image base — and the deliberate Debian divergences from ``rhel``: no ``/etc/selinux/config``
edit, no NetworkManager keyfile, ``ssh.service`` not ``sshd.service``.
"""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.base import CustomizeContext
from kdive.images.families.debian import DebianFamily


def _ctx(
    tmp_path: Path,
    *,
    is_cloud_image: bool,
    kind: str = "debug",
    distro: str = "debian",
    version: str = "12",
) -> CustomizeContext:
    fam = DebianFamily()
    return CustomizeContext(
        kind=kind,
        packages=fam.packages(kind, distro, version),
        authorized_key=tmp_path / "key.pub",
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro=distro,
        version=version,
    )


def test_family_identity_and_kdump_unit() -> None:
    fam = DebianFamily()
    assert fam.family == "debian"
    assert fam.kdump_unit == "kdump-tools.service", "Debian's kdump unit is kdump-tools.service"
    assert fam.guest_mac == "apparmor", "Debian uses AppArmor, not SELinux"


def test_debug_packages_are_the_apt_crash_set() -> None:
    pkgs = DebianFamily().packages("debug", "debian", "12")
    # apt names: kdump-tools (not kdump-utils/kexec-tools), python3-drgn (not drgn), crash.
    assert "kdump-tools" in pkgs
    assert "makedumpfile" in pkgs
    assert "python3-drgn" in pkgs, "drgn ships as python3-drgn on Debian"
    assert "openssh-server" in pkgs
    assert "crash" in pkgs
    assert "drgn" not in pkgs and "kdump-utils" not in pkgs and "kexec-tools" not in pkgs


def test_build_packages_are_the_toolchain_set() -> None:
    pkgs = DebianFamily().packages("build", "debian", "12")
    assert "gcc" in pkgs and "make" in pkgs
    # Debian -dev package names diverge from Fedora's -devel.
    assert "libssl-dev" in pkgs and "libelf-dev" in pkgs
    assert "kdump-tools" not in pkgs and "makedumpfile" not in pkgs


def test_debug_argv_enables_ssh_and_kdump_tools(tmp_path: Path) -> None:
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "systemctl enable ssh.service" in argv, "Debian's sshd unit is ssh.service"
    assert "systemctl enable sshd.service" not in argv
    assert "systemctl enable kdump-tools.service" in argv
    # USE_KDUMP=1 is required or kdump-tools.service no-ops.
    assert "USE_KDUMP=1" in j and "/etc/default/kdump-tools" in j
    assert "99-kdive-kdump.conf" in j and "unknown_nmi_panic=1" in j


def test_debug_argv_injects_key_and_readiness_unit(tmp_path: Path) -> None:
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert f"root:file:{tmp_path / 'key.pub'}" in j
    assert "systemctl enable kdive-ready.service" in argv


def test_debug_argv_stages_sshd_host_key_generation(tmp_path: Path) -> None:
    # Debian genericcloud ships openssh-server with NO host keys (cloud-init makes them per-instance
    # at first boot); disabling cloud-init removes that, and Debian has no Fedora/RHEL sshd-keygen
    # unit, so ssh.service fails its `sshd -t` preflight. Stage a oneshot that runs `ssh-keygen -A`
    # before ssh.service so SSH (the drgn-live transport) comes up (#824, live-found).
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "ssh-keygen -A" in j, "must generate missing sshd host keys"
    assert "Before=ssh.service" in j, "keygen must be ordered before ssh.service"
    assert "systemctl enable kdive-sshd-keygen.service" in argv


def test_debug_argv_touches_no_selinux_and_stages_no_nm_keyfile(tmp_path: Path) -> None:
    # Debian has no /etc/selinux/config and no NetworkManager — neither must be touched (#824).
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "selinux" not in j.lower()
    assert "NetworkManager" not in j and "kdive-ssh-nic" not in j


def test_debug_argv_stages_kdive_drgn_helper(tmp_path: Path) -> None:
    # The live introspect path SSH-execs /usr/local/sbin/kdive-drgn; python3-drgn ships the drgn CLI
    # so `drgn -k` works. The debug image must carry the reviewed helper, read-executable.
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    assert any(a.endswith(":/usr/local/sbin/kdive-drgn") for a in argv)
    assert "chmod 0755 /usr/local/sbin/kdive-drgn" in argv


def test_cloud_image_disables_cloud_init_version_proof_and_seeds_machine_id(tmp_path: Path) -> None:
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    # version-proof across the Debian 13 cloud-init unit rename: the disable file, not unit masks.
    assert "/etc/cloud/cloud-init.disabled" in j
    assert "/etc/machine-id" in j, "seed machine-id so first-boot preset-all keeps kdump enabled"


def test_virt_builder_source_skips_cloud_init_and_machine_id(tmp_path: Path) -> None:
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=False))
    j = " ".join(argv)
    assert "cloud-init.disabled" not in j
    assert "/etc/machine-id" not in j


def test_build_argv_omits_kdump_nmi_and_drgn_helper(tmp_path: Path) -> None:
    # A build-host image never runs force_crash and carries no introspection contract.
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True, kind="build"))
    j = " ".join(argv)
    assert "kdump-tools.service" not in j
    assert "unknown_nmi_panic" not in j
    assert "kdive-drgn" not in j


def test_normalize_writes_fstab_removes_crypttab_no_selinux(tmp_path: Path) -> None:
    # The debian normalize rewrites fstab + drops crypttab; AppArmor needs no relabel and there is
    # no /etc/selinux/config to touch (#824). Capture the guestfish script via an injected runner.
    scripts: list[str] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> None:
        scripts.append(str(kwargs.get("input_text", "")))

    DebianFamily().normalize(tmp_path / "img.qcow2", _run_guestfs=_fake_run_guestfs)
    assert len(scripts) == 1
    script = scripts[0]
    assert "/etc/fstab" in script and "rm-f /etc/crypttab" in script
    assert "selinux" not in script.lower()
    assert "autorelabel" not in script
