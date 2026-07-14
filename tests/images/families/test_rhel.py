"""Unit tests for the rhel FamilyCustomizer argv contract (ADR-0251, #823).

These pin the virt-customize argv the rhel customizer builds without running libguestfs: the
PROVEN Fedora-44 customization (kdump + sshd enable, NMI-panic sysctl, kdive-ready unit, SELinux
permissive) plus the cloud-init first-boot baking (ADR-0288), and the EL-major-aware package
divergence (#823): EL 8/9 take makedumpfile/kdumpctl from ``kexec-tools`` and EL 8 enables EPEL
for ``drgn``. The image bakes no authorized key (ADR-0289, #963).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kdive.images.families.base import CustomizeContext
from kdive.images.families.renderers import render_argv
from kdive.images.families.rhel import RhelFamily
from kdive.images.planes._build_common import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)


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
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro=distro,
        version=version,
    )


def _argv(ctx: CustomizeContext) -> list[str]:
    """Render the rhel family's steps to the virt-customize argv the tests pin (ADR-0345)."""
    return render_argv(RhelFamily().customize_steps(ctx), cleanup=[])


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
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdump-utils" in j and "makedumpfile" in j
    assert "systemctl enable kdump.service" in argv
    assert "systemctl enable sshd.service" in argv
    assert "99-kdive-kdump.conf" in j and "unknown_nmi_panic=1" in j
    assert "final_action poweroff" in j


def test_debug_argv_writes_makedumpfile_version_marker(tmp_path: Path) -> None:
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in " ".join(argv)


def test_build_argv_omits_makedumpfile_version_marker(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, is_cloud_image=True)
    build_ctx = replace(ctx, kind="build", packages=RhelFamily().packages("build", "fedora", "44"))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH not in " ".join(_argv(build_ctx))


def test_debug_argv_writes_drgn_version_marker(tmp_path: Path) -> None:
    # drgn is in every rhel/fedora debug set, so the drgn-version marker is written (ADR-0334).
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    joined = " ".join(argv)
    assert DRGN_MARKER_GUEST_PATH in joined
    assert "drgn --version" in joined


def test_build_argv_omits_drgn_version_marker(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, is_cloud_image=True)
    build_ctx = replace(ctx, kind="build", packages=RhelFamily().packages("build", "fedora", "44"))
    assert DRGN_MARKER_GUEST_PATH not in " ".join(_argv(build_ctx))


def test_sshd_enable_is_coupled_to_the_debug_kind(tmp_path: Path) -> None:
    # sshd enablement mirrors the SSH capability, which capabilities() ties to kind: a debug image
    # enables sshd.service, a build-host image (which declares no SSH) never does.
    ctx = _ctx(tmp_path, is_cloud_image=True)
    build_ctx = replace(ctx, kind="build", packages=RhelFamily().packages("build", "fedora", "44"))
    assert "systemctl enable sshd.service" in _argv(ctx)
    assert "systemctl enable sshd.service" not in _argv(build_ctx)


def test_el9_debug_argv_enables_kdump_without_kdump_utils(tmp_path: Path) -> None:
    """EL9 has no kdump-utils pkg; kdump-enable must gate on kexec-tools, not kdump-utils."""
    argv = _argv(_ctx(tmp_path, is_cloud_image=True, distro="rocky", version="9"))
    j = " ".join(argv)
    installed = argv[argv.index("--install") + 1]
    assert "kdump-utils" not in installed and "makedumpfile" not in installed
    assert "systemctl enable kdump.service" in argv
    assert "final_action poweroff" in j


def test_el8_debug_argv_enables_epel_before_installing_drgn(tmp_path: Path) -> None:
    argv = _argv(_ctx(tmp_path, is_cloud_image=True, distro="rocky", version="8"))
    assert "dnf -y install epel-release" in argv
    epel_idx = argv.index("dnf -y install epel-release")
    install_idx = next(i for i, a in enumerate(argv) if a.startswith("drgn,") or ",drgn" in a)
    assert epel_idx < install_idx, "EPEL must be enabled before the drgn install transaction"
    assert "systemctl enable kdump.service" in argv


def test_el9_and_el10_do_not_enable_epel(tmp_path: Path) -> None:
    for distro, version in (("rocky", "9"), ("rocky", "10"), ("centos-stream", "10")):
        argv = _argv(_ctx(tmp_path, is_cloud_image=True, distro=distro, version=version))
        assert "dnf -y install epel-release" not in argv, (distro, version)


def test_rhel_debug_argv_omits_ssh_inject_and_stages_readiness_unit(tmp_path: Path) -> None:
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "--ssh-inject" not in argv
    assert "root:file:" not in j
    assert "systemctl enable kdive-ready.service" in argv
    assert any("SELINUX" in a and "permissive" in a for a in argv)


def test_rhel_argv_stages_no_nm_ssh_nic_keyfile(tmp_path: Path) -> None:
    # ADR-0288: cloud-init DHCPs the NIC now; the NetworkManager SSH-NIC keyfile is gone.
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdive-ssh-nic" not in j
    assert "NetworkManager/system-connections" not in j


def test_rhel_argv_bakes_cloud_init_and_stops_masking(tmp_path: Path) -> None:
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in j  # authoritative drop-in
    assert "rm -f /etc/cloud/cloud-init.disabled" in j  # undoes any cloud-init disable
    assert "systemctl mask cloud-init" not in j  # no longer masked


def test_rhel_argv_still_omits_key_inject_and_keeps_selinux(tmp_path: Path) -> None:
    # Anti-regression: no baked key (ADR-0289, #963); the SELinux permissive edit stays.
    argv = _argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "--ssh-inject" not in argv
    assert "SELINUX=permissive" in j


def test_rhel_virt_builder_base_installs_cloud_init(tmp_path: Path) -> None:
    # A non-cloud (virt-builder) base ships no cloud-init; the family must install it so the
    # baked NoCloud seed applies uniformly (ADR-0288).
    argv = _argv(_ctx(tmp_path, is_cloud_image=False))
    assert "--install cloud-init" in " ".join(argv)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in " ".join(argv)
