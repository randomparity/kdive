"""Unit tests for the debian FamilyCustomizer step contract (ADR-0251, #824, ADR-0288, #1167).

These pin the customization steps the debian customizer emits without running libguestfs or a
customization boot: the apt index refresh + install, ``ssh.service``/``kdump-tools.service``
enable, ``USE_KDUMP=1``, the NMI-panic sysctl, the kdive-ready unit, and the shared cloud-init
first-boot baking (ADR-0288) — and the deliberate Debian divergences from ``rhel``: no
``/etc/selinux/config`` edit, no NetworkManager keyfile, ``ssh.service`` not ``sshd.service``. The
image bakes no authorized key (ADR-0289, #963).
"""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.base import CustomizeContext
from kdive.images.families.debian import DebianFamily
from kdive.images.families.steps import InstallPackages, RunCommand, Step
from kdive.images.planes._build_common import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)
from kdive.images.rootfs.kinds import RootfsImageKind
from tests.support.customize_steps import (
    baked_paths,
    commands,
    installed,
    rendered,
    upload_source,
)


def _ctx(
    tmp_path: Path,
    *,
    is_cloud_image: bool,
    kind: RootfsImageKind = "debug",
    distro: str = "debian",
    version: str = "12",
) -> CustomizeContext:
    fam = DebianFamily()
    return CustomizeContext(
        kind=kind,
        packages=fam.packages(kind, distro, version),
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        distro=distro,
        version=version,
        fadump_capture=False,
    )


def _steps(ctx: CustomizeContext) -> list[Step]:
    return DebianFamily().customize_steps(ctx)


def test_debug_steps_write_makedumpfile_version_marker(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True, kind="debug"))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in rendered(steps)


def test_build_steps_omit_makedumpfile_version_marker(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True, kind="build"))
    assert MAKEDUMPFILE_MARKER_GUEST_PATH not in rendered(steps)


def test_debug_steps_write_drgn_version_marker(tmp_path: Path) -> None:
    # python3-drgn is in the debug set, so the drgn-version marker is written (ADR-0334).
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True, kind="debug")))
    assert DRGN_MARKER_GUEST_PATH in text
    assert "drgn --version" in text


def test_build_steps_omit_drgn_version_marker(tmp_path: Path) -> None:
    # A build-host image installs no python3-drgn, so no drgn marker is written.
    steps = _steps(_ctx(tmp_path, is_cloud_image=True, kind="build"))
    assert DRGN_MARKER_GUEST_PATH not in rendered(steps)


def test_family_identity_kdump_unit_and_package_manager() -> None:
    fam = DebianFamily()
    assert fam.family == "debian"
    assert fam.kdump_unit == "kdump-tools.service", "Debian's kdump unit is kdump-tools.service"
    assert fam.guest_mac == "apparmor", "Debian uses AppArmor, not SELinux"
    # The firstboot script installs with apt, non-interactively (a debconf prompt would hang the
    # customization boot), and the family — not the renderer — owns that choice (#1167).
    assert fam.install_command.endswith("apt-get -y install")
    assert "DEBIAN_FRONTEND=noninteractive" in fam.install_command


def test_apt_index_refresh_precedes_the_install(tmp_path: Path) -> None:
    """A cloud base ships no usable apt index, so the guest refreshes it before installing."""
    ctx = _ctx(tmp_path, is_cloud_image=True)
    steps = _steps(ctx)
    assert steps[0] == RunCommand("apt-get update")
    assert steps[1] == InstallPackages(ctx.packages)


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


def test_debug_steps_enable_ssh_and_kdump_tools(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    cmds = commands(steps)
    text = rendered(steps)
    assert "systemctl enable ssh.service" in cmds, "Debian's sshd unit is ssh.service"
    assert "systemctl enable sshd.service" not in cmds
    assert "systemctl enable kdump-tools.service" in cmds
    # USE_KDUMP=1 is required or kdump-tools.service no-ops.
    assert "USE_KDUMP=1" in text and "/etc/default/kdump-tools" in text
    assert "99-kdive-kdump.conf" in text and "unknown_nmi_panic=1" in text


def test_debug_steps_omit_ssh_inject_and_stage_readiness_unit(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    text = rendered(steps)
    assert "ssh-inject" not in text
    assert "root:file:" not in text
    assert "systemctl enable kdive-ready.service" in commands(steps)


def test_debian_steps_bake_cloud_init_and_drop_sshd_keygen(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    text = rendered(steps)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in baked_paths(steps)
    assert "rm -f /etc/cloud/cloud-init.disabled" in commands(steps)  # undoes any disable
    assert "/etc/cloud/cloud-init.disabled" not in baked_paths(steps)  # no longer disabled
    assert "kdive-sshd-keygen" not in text  # cloud-init generates host keys
    assert "ssh-keygen -A" not in text


def test_debug_steps_touch_no_selinux_and_stage_no_nm_keyfile(tmp_path: Path) -> None:
    # Debian has no /etc/selinux/config and no NetworkManager — neither must be touched (#824).
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True)))
    assert "selinux" not in text.lower()
    assert "NetworkManager" not in text and "kdive-ssh-nic" not in text


def test_debug_steps_stage_kdive_drgn_helper(tmp_path: Path) -> None:
    # The live introspect path SSH-execs /usr/local/sbin/kdive-drgn; python3-drgn ships the drgn CLI
    # so `drgn -k` works. The debug image must carry the reviewed helper, read-executable.
    steps = _steps(_ctx(tmp_path, is_cloud_image=True))
    assert upload_source(steps, "/usr/local/sbin/kdive-drgn").is_file()
    assert "chmod 0755 /usr/local/sbin/kdive-drgn" in rendered(steps)


def test_virt_builder_base_installs_cloud_init_and_seeds_machine_id(tmp_path: Path) -> None:
    steps = _steps(_ctx(tmp_path, is_cloud_image=False))
    assert "cloud-init" in installed(steps)
    assert "/etc/machine-id" in baked_paths(steps)


def test_build_steps_omit_kdump_nmi_and_drgn_helper(tmp_path: Path) -> None:
    # A build-host image never runs force_crash and carries no introspection contract.
    text = rendered(_steps(_ctx(tmp_path, is_cloud_image=True, kind="build")))
    assert "kdump-tools.service" not in text
    assert "unknown_nmi_panic" not in text
    assert "kdive-drgn" not in text


def test_ssh_enable_is_coupled_to_the_debug_kind(tmp_path: Path) -> None:
    # sshd enablement mirrors the SSH capability, which capabilities() ties to kind: a debug image
    # enables ssh.service, a build-host image (which declares no SSH) never does.
    debug = commands(_steps(_ctx(tmp_path, is_cloud_image=True, kind="debug")))
    build = commands(_steps(_ctx(tmp_path, is_cloud_image=True, kind="build")))
    assert "systemctl enable ssh.service" in debug
    assert "systemctl enable ssh.service" not in build


def test_normalize_writes_fstab_removes_crypttab_no_selinux(tmp_path: Path) -> None:
    # The debian normalize rewrites fstab + drops crypttab; AppArmor needs no relabel and there is
    # no /etc/selinux/config to touch (#824). Capture the guestfish script via an injected runner.
    scripts: list[str] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> str:
        scripts.append(str(kwargs.get("input_text", "")))
        return ""

    DebianFamily().normalize(tmp_path / "img.qcow2", _run_guestfs=_fake_run_guestfs)
    assert len(scripts) == 1
    script = scripts[0]
    assert "/etc/fstab" in script and "rm-f /etc/crypttab" in script
    assert "selinux" not in script.lower()
    assert "autorelabel" not in script


def test_normalize_scales_its_guestfish_budget_on_an_emulated_host(tmp_path: Path) -> None:
    # Sibling of the stage that failed the #2414 measurement run: with virt-tar-out fixed,
    # in-guest build-fs on an emulated-POWER host died in the rhel family's guestfish
    # normalization at 303 s against an unscaled 300 s budget. Debian shares the base and the
    # appliance, so it must scale the same way.
    import kdive.config as config
    from kdive.images.families import debian as debian_module

    budgets: list[int] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> str:
        timeout_s = kwargs["timeout_s"]
        assert isinstance(timeout_s, int)  # run_guestfs_tool's declared contract
        budgets.append(timeout_s)
        return ""

    config.load(
        {
            "KDIVE_KVM_NODE": str(tmp_path / "absent"),
            "KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER": "10.0",
        }
    )
    try:
        DebianFamily().normalize(tmp_path / "img.qcow2", _run_guestfs=_fake_run_guestfs)
    finally:
        config.reset()

    assert budgets == [debian_module._GUESTFISH_TIMEOUT_S * 10]
    assert budgets[0] > 303
