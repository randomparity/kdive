"""Unit tests for the rhel FamilyCustomizer step contract (ADR-0251, #823).

These pin the customization steps the rhel customizer emits without running libguestfs or a
customization boot: the PROVEN Fedora-44 customization (kdump + sshd enable, NMI-panic sysctl,
kdive-ready unit, SELinux permissive) plus the cloud-init first-boot baking (ADR-0288), and the
EL-major-aware package divergence (#823): EL 8/9 take makedumpfile/kdumpctl from ``kexec-tools``
and every EL clone enables EPEL for ``drgn``. The image bakes no authorized key (ADR-0289, #963).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kdive.images.families.base import CustomizeContext
from kdive.images.families.rhel import RhelFamily
from kdive.images.families.steps import InstallPackages, RunCommand, Step
from kdive.images.planes._build_common import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)
from tests.support.customize_steps import baked_paths, commands, installed, rendered


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
        distro=distro,
        version=version,
    )


def _steps(ctx: CustomizeContext) -> list[Step]:
    return RhelFamily().customize_steps(ctx)


def _build_ctx(ctx: CustomizeContext) -> CustomizeContext:
    return replace(ctx, kind="build", packages=RhelFamily().packages("build", "fedora", "44"))


def test_install_command_is_non_interactive_dnf() -> None:
    assert RhelFamily().install_command == "dnf -y install"


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


def test_fedora_debug_steps_enable_kdump_and_sshd(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    pkgs = installed(steps)
    assert "kdump-utils" in pkgs and "makedumpfile" in pkgs
    cmds = commands(steps)
    assert "systemctl enable kdump.service" in cmds
    assert "systemctl enable sshd.service" in cmds
    text = rendered(steps)
    assert "99-kdive-kdump.conf" in text and "unknown_nmi_panic=1" in text
    assert "final_action poweroff" in text


def test_debug_steps_install_fadump_capture_service(tmp_path: Path) -> None:
    # fadump-capture.service is written to the image and enabled on every debug image that carries
    # kexec-tools (Fedora and all EL); ConditionPathExists=/proc/vmcore keeps it a no-op on normal
    # boots so it is safe on x86_64 too.  Declared per AGENTS.md parity rule (#2381, proved #2312).
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    text = rendered(steps)
    assert "fadump-capture.service" in text
    assert "systemctl enable fadump-capture.service" in commands(steps)


def test_debug_steps_write_makedumpfile_version_marker(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in rendered(steps)


def test_build_steps_omit_makedumpfile_version_marker(tmp_path: Path) -> None:
    steps = _steps(_build_ctx(_ctx(tmp_path, is_cloud_image=True)))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH not in rendered(steps)


def test_debug_steps_write_drgn_version_marker(tmp_path: Path) -> None:
    # drgn is in every rhel/fedora debug set, so the drgn-version marker is written (ADR-0334).
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True)))
    assert DRGN_MARKER_GUEST_PATH in text
    assert "drgn --version" in text


def test_build_steps_omit_drgn_version_marker(tmp_path: Path) -> None:
    steps = _steps(_build_ctx(_ctx(tmp_path, is_cloud_image=True)))
    assert DRGN_MARKER_GUEST_PATH not in rendered(steps)


def test_sshd_enable_is_coupled_to_the_debug_kind(tmp_path: Path) -> None:
    # sshd enablement mirrors the SSH capability, which capabilities() ties to kind: a debug image
    # enables sshd.service, a build-host image (which declares no SSH) never does.
    ctx = _ctx(tmp_path, is_cloud_image=True)
    assert "systemctl enable sshd.service" in commands(_steps(ctx))
    assert "systemctl enable sshd.service" not in commands(_steps(_build_ctx(ctx)))


def test_el9_debug_steps_enable_kdump_without_kdump_utils(tmp_path: Path) -> None:
    """EL9 has no kdump-utils pkg; kdump-enable must gate on kexec-tools, not kdump-utils."""
    steps = _steps(_ctx(tmp_path, is_cloud_image=True, distro="rocky", version="9"))
    pkgs = installed(steps)
    assert "kdump-utils" not in pkgs and "makedumpfile" not in pkgs
    assert "systemctl enable kdump.service" in commands(steps)
    assert "final_action poweroff" in rendered(steps)


def test_el_clones_enable_epel_before_installing_drgn(tmp_path: Path) -> None:
    """Every EL clone (8/9/10) sources ``drgn`` from EPEL, so EPEL is enabled before the drgn
    install transaction. EL9/EL10 were previously left out — the customize boot never ran a real EL9
    image until the #1152 CentOS Stream 9 ppc64le proof, and EL10 takes drgn from EPEL 10 too
    (ADR-0350, #1152).
    """
    for distro, version in (
        ("rocky", "8"),
        ("rocky", "9"),
        ("centos-stream", "9"),
        ("rocky", "10"),
        ("centos-stream", "10"),
    ):
        steps = _steps(_ctx(tmp_path, is_cloud_image=True, distro=distro, version=version))
        epel_idx = steps.index(RunCommand("dnf -y install epel-release"))
        install_idx = next(
            i for i, s in enumerate(steps) if isinstance(s, InstallPackages) and "drgn" in s.names
        )
        assert epel_idx < install_idx, (distro, version)
        assert "systemctl enable kdump.service" in commands(steps), (distro, version)


def test_fedora_does_not_enable_epel(tmp_path: Path) -> None:
    """Fedora ships ``drgn`` in its base repo (no EPEL), so the customizer must not enable EPEL."""
    steps = _steps(_ctx(tmp_path, is_cloud_image=True, distro="fedora", version="44"))
    assert "dnf -y install epel-release" not in commands(steps)


def test_rhel_debug_steps_omit_ssh_inject_and_stage_readiness_unit(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    text = rendered(steps)
    assert "ssh-inject" not in text
    assert "root:file:" not in text
    cmds = commands(steps)
    assert "systemctl enable kdive-ready.service" in cmds
    assert any("SELINUX" in c and "permissive" in c for c in cmds)


def test_rhel_steps_stage_no_nm_ssh_nic_keyfile(tmp_path: Path) -> None:
    # ADR-0288: cloud-init DHCPs the NIC now; the NetworkManager SSH-NIC keyfile is gone.
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True)))
    assert "kdive-ssh-nic" not in text
    assert "NetworkManager/system-connections" not in text


def test_rhel_steps_bake_cloud_init_and_stop_masking(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in baked_paths(steps)  # authoritative drop-in
    assert "rm -f /etc/cloud/cloud-init.disabled" in commands(steps)  # undoes any disable
    assert "systemctl mask cloud-init" not in rendered(steps)  # no longer masked


def test_rhel_steps_still_omit_key_inject_and_keep_selinux(tmp_path: Path) -> None:
    # Anti-regression: no baked key (ADR-0289, #963); the SELinux permissive edit stays.
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True)))
    assert "ssh-inject" not in text
    assert "SELINUX=permissive" in text


def test_rhel_virt_builder_base_installs_cloud_init(tmp_path: Path) -> None:
    # A non-cloud (virt-builder) base ships no cloud-init; the family must install it so the
    # baked NoCloud seed applies uniformly (ADR-0288).
    steps = _steps(_ctx(tmp_path, is_cloud_image=False))
    assert "cloud-init" in installed(steps)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in baked_paths(steps)


def test_normalize_sets_permissive_without_touching_autorelabel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first-boot relabel is the offline seal's job after the customization boot (ADR-0345):
    # normalize must set SELINUX=permissive and rewrite fstab/crypttab but never /.autorelabel.
    from kdive.images.families import rhel as rhel_module

    scripts: list[str] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> str:
        scripts.append(str(kwargs.get("input_text", "")))
        return ""

    monkeypatch.setattr(rhel_module, "run_guestfs_tool", _fake_run_guestfs)
    RhelFamily().normalize(tmp_path / "img.qcow2")
    assert len(scripts) == 1
    script = scripts[0]
    assert "/etc/fstab" in script and "/etc/selinux/config" in script
    assert "rm-f /etc/crypttab" in script
    assert "autorelabel" not in script
