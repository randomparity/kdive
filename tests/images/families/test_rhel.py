"""Unit tests for the rhel FamilyCustomizer argv contract (ADR-0250).

These pin the virt-customize argv the rhel customizer builds without running libguestfs: the
PROVEN Fedora-44 customization (kdump + sshd enable, NMI-panic sysctl, ssh-inject, kdive-ready
unit, SELinux permissive) plus the cloud-image-only cloud-init mask and ``/etc/machine-id`` seed.
"""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.base import CustomizeContext
from kdive.images.families.rhel import RhelFamily


def _ctx(tmp_path: Path, *, is_cloud_image: bool) -> CustomizeContext:
    fam = RhelFamily()
    return CustomizeContext(
        kind="debug",
        packages=fam.packages("debug"),
        authorized_key=tmp_path / "key.pub",
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
    )


def test_rhel_debug_packages_include_kdump_and_openssh() -> None:
    pkgs = RhelFamily().packages("debug")
    assert "kdump-utils" in pkgs and "makedumpfile" in pkgs and "openssh-server" in pkgs


def test_rhel_build_packages_are_the_toolchain_set() -> None:
    pkgs = RhelFamily().packages("build")
    assert "gcc" in pkgs and "make" in pkgs and "kdump-utils" not in pkgs


def test_rhel_debug_argv_enables_kdump_and_sshd(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdump-utils" in j and "makedumpfile" in j
    assert "systemctl enable kdump.service" in argv
    assert "systemctl enable sshd.service" in argv
    assert "99-kdive-kdump.conf" in j and "unknown_nmi_panic=1" in j
    assert "final_action poweroff" in j


def test_rhel_debug_argv_injects_key_and_readiness_unit(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert f"root:file:{tmp_path / 'key.pub'}" in j
    assert "systemctl enable kdive-ready.service" in argv
    assert any("SELINUX" in a and "permissive" in a for a in argv)


def test_rhel_cloud_image_disables_cloud_init_and_seeds_machine_id(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    assert any("cloud-init" in a for a in argv)
    # machine-id seed (else first-boot preset-all disables kdump on Fedora Cloud)
    assert any("/etc/machine-id" in a for a in argv)


def test_rhel_virt_builder_source_skips_machine_id_seed(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=False))
    assert not any("/etc/machine-id" in a for a in argv)


def test_rhel_virt_builder_source_skips_cloud_init(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=False))
    assert not any("cloud-init" in a for a in argv)
