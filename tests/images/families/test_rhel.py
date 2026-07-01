"""Unit tests for the rhel FamilyCustomizer argv contract (ADR-0251, #823).

These pin the virt-customize argv the rhel customizer builds without running libguestfs: the
PROVEN Fedora-44 customization (kdump + sshd enable, NMI-panic sysctl, ssh-inject, kdive-ready
unit, SELinux permissive) plus the cloud-image-only cloud-init mask and ``/etc/machine-id`` seed,
and the EL-major-aware package divergence (#823): EL 8/9 take makedumpfile/kdumpctl from
``kexec-tools`` and EL 8 enables EPEL for ``drgn``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kdive.images.families.base import CustomizeContext
from kdive.images.families.rhel import RhelFamily
from kdive.images.planes._build_common import MAKEDUMPFILE_MARKER_GUEST_PATH


def _ctx(
    tmp_path: Path,
    *,
    is_cloud_image: bool,
    distro: str = "fedora",
    version: str = "44",
) -> CustomizeContext:
    fam = RhelFamily()
    return CustomizeContext(
        kind="debug",
        packages=fam.packages("debug", distro, version),
        authorized_key=tmp_path / "key.pub",
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro=distro,
        version=version,
    )


def test_fedora_and_el10_debug_packages_have_separate_makedumpfile() -> None:
    for distro, version in (("fedora", "44"), ("rocky", "10"), ("centos-stream", "10")):
        pkgs = RhelFamily().packages("debug", distro, version)
        assert "makedumpfile" in pkgs, (distro, version)
        assert "kdump-utils" in pkgs, (distro, version)
        assert "drgn" in pkgs and "openssh-server" in pkgs and "kexec-tools" in pkgs
        # keyutils provides keyctl, which kdumpctl invokes building the crash env (ADR-0213, #688).
        assert "keyutils" in pkgs, (distro, version)


def test_el8_el9_debug_packages_drop_separate_makedumpfile_and_kdump_utils() -> None:
    for distro, version in (("rocky", "8"), ("rocky", "9"), ("centos-stream", "9")):
        pkgs = RhelFamily().packages("debug", distro, version)
        # makedumpfile + kdumpctl are bundled in kexec-tools on EL8/9 — the standalone packages
        # do not exist, so installing them by name would fail the build.
        assert "makedumpfile" not in pkgs, (distro, version)
        assert "kdump-utils" not in pkgs, (distro, version)
        assert "kexec-tools" in pkgs and "drgn" in pkgs and "openssh-server" in pkgs


def test_build_packages_are_the_toolchain_set_on_every_release() -> None:
    for distro, version in (("fedora", "44"), ("rocky", "8"), ("rocky", "10")):
        pkgs = RhelFamily().packages("build", distro, version)
        assert "gcc" in pkgs and "make" in pkgs
        assert "kdump-utils" not in pkgs and "kexec-tools" not in pkgs


def test_fedora_debug_argv_enables_kdump_and_sshd(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdump-utils" in j and "makedumpfile" in j
    assert "systemctl enable kdump.service" in argv
    assert "systemctl enable sshd.service" in argv
    assert "99-kdive-kdump.conf" in j and "unknown_nmi_panic=1" in j
    assert "final_action poweroff" in j


def test_debug_argv_writes_makedumpfile_version_marker(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in " ".join(argv)


def test_build_argv_omits_makedumpfile_version_marker(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, is_cloud_image=True)
    build_ctx = replace(ctx, kind="build", packages=RhelFamily().packages("build", "fedora", "44"))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH not in " ".join(RhelFamily().customize_argv(build_ctx))


def test_el9_debug_argv_enables_kdump_without_kdump_utils(tmp_path: Path) -> None:
    """EL9 has no kdump-utils pkg; kdump-enable must gate on kexec-tools, not kdump-utils."""
    argv = RhelFamily().customize_argv(
        _ctx(tmp_path, is_cloud_image=True, distro="rocky", version="9")
    )
    j = " ".join(argv)
    installed = argv[argv.index("--install") + 1]
    assert "kdump-utils" not in installed and "makedumpfile" not in installed
    assert "systemctl enable kdump.service" in argv
    assert "final_action poweroff" in j


def test_el8_debug_argv_enables_epel_before_installing_drgn(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(
        _ctx(tmp_path, is_cloud_image=True, distro="rocky", version="8")
    )
    assert "dnf -y install epel-release" in argv
    epel_idx = argv.index("dnf -y install epel-release")
    install_idx = next(i for i, a in enumerate(argv) if a.startswith("drgn,") or ",drgn" in a)
    assert epel_idx < install_idx, "EPEL must be enabled before the drgn install transaction"
    assert "systemctl enable kdump.service" in argv


def test_el9_and_el10_do_not_enable_epel(tmp_path: Path) -> None:
    for distro, version in (("rocky", "9"), ("rocky", "10"), ("centos-stream", "10")):
        argv = RhelFamily().customize_argv(
            _ctx(tmp_path, is_cloud_image=True, distro=distro, version=version)
        )
        assert "dnf -y install epel-release" not in argv, (distro, version)


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


def test_rhel_argv_stages_no_nm_ssh_nic_keyfile(tmp_path: Path) -> None:
    # ADR-0288: cloud-init DHCPs the NIC now; the NetworkManager SSH-NIC keyfile is gone.
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdive-ssh-nic" not in j
    assert "NetworkManager/system-connections" not in j
