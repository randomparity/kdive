"""Unit tests for the SUSE rootfs FamilyCustomizer contract (#825)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families import family_for
from kdive.images.families._fedora_customize import KDIVE_CLOUD_CFG_PATH
from kdive.images.families.base import CustomizeContext, FamilyCustomizer
from kdive.images.families.steps import InstallPackages, Mkdir, RunCommand, StageFile, Step
from kdive.images.families.suse import SuseFamily
from kdive.images.planes._build_common import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)
from tests.support.customize_steps import baked_contents, commands, installed, rendered

_POST_PATH = "/usr/local/sbin/kdive-suse-kdump-post"
_DUMP_ROOT = "/kdump/mnt/var/crash"
_READINESS_DROPIN_PATH = "/etc/systemd/system/kdive-ready.service.d/suse.conf"
_READINESS_DROPIN_DIR = "/etc/systemd/system/kdive-ready.service.d"


def _family() -> FamilyCustomizer:
    return family_for("suse")


def _ctx(tmp_path: Path, distro: str = "opensuse-tumbleweed") -> CustomizeContext:
    family = _family()
    readiness = tmp_path / "kdive-ready.service"
    readiness.write_text("[Unit]\n", encoding="utf-8")
    return CustomizeContext(
        kind="debug",
        packages=family.packages("debug", distro, "15.6"),
        readiness_unit_path=readiness,
        is_cloud_image=True,
        distro=distro,
        version="15.6",
        fadump_capture=False,
    )


def _steps(tmp_path: Path, distro: str = "opensuse-tumbleweed") -> list[Step]:
    return _family().customize_steps(_ctx(tmp_path, distro))


def test_registry_resolves_suse_family() -> None:
    family = _family()
    assert family.family == "suse"
    assert family.kdump_unit == "kdump.service"
    assert family.guest_mac == "apparmor"
    assert family.install_command == "zypper --non-interactive install --no-recommends"


@pytest.mark.parametrize("method", ["packages", "capabilities"])
def test_build_images_are_rejected(method: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        getattr(_family(), method)("build", "opensuse-tumbleweed", "20260920")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "kind"


@pytest.mark.parametrize("method", ["packages", "capabilities"])
def test_unknown_suse_distro_is_rejected(method: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        getattr(_family(), method)("debug", "sles", "15.6")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "distro"


def test_package_and_capability_sets_diverge_only_for_drgn() -> None:
    family = _family()
    tumbleweed = family.packages("debug", "opensuse-tumbleweed", "20260920")
    leap = family.packages("debug", "opensuse-leap", "15.6")
    common = {"kdump", "kexec-tools", "makedumpfile", "dracut", "crash", "openssh-server"}
    assert common <= set(tumbleweed)
    assert common <= set(leap)
    assert "drgn" in tumbleweed
    assert "drgn" not in leap
    assert set(family.capabilities("debug", "opensuse-tumbleweed", "20260920")) == {
        Capability.SSH,
        Capability.APPARMOR,
        Capability.KDUMP,
        Capability.DRGN,
    }
    assert set(family.capabilities("debug", "opensuse-leap", "15.6")) == {
        Capability.SSH,
        Capability.APPARMOR,
        Capability.KDUMP,
    }


def test_zypper_refresh_precedes_install_and_services(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    steps = _family().customize_steps(ctx)
    assert steps[0] == RunCommand("zypper --non-interactive refresh")
    assert steps[1] == InstallPackages(ctx.packages)
    cmds = commands(steps)
    assert "systemctl enable sshd.service" in cmds
    assert "systemctl enable kdump.service" in cmds
    assert "systemctl enable kdive-ready.service" in cmds
    text = rendered(steps)
    assert "unknown_nmi_panic=1" in text
    assert text.index("install ") < text.index("/etc/sysconfig/kdump")


def test_steps_bake_cloud_init_and_conditional_provenance(tmp_path: Path) -> None:
    tumbleweed = _steps(tmp_path)
    leap = _steps(tmp_path, "opensuse-leap")
    tumbleweed_text = rendered(tumbleweed)
    leap_text = rendered(leap)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in tumbleweed_text
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in tumbleweed_text
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in leap_text
    assert DRGN_MARKER_GUEST_PATH in tumbleweed_text
    assert DRGN_MARKER_GUEST_PATH not in leap_text
    assert _POST_PATH in tumbleweed_text and _POST_PATH in leap_text
    assert "kdive-drgn" in tumbleweed_text
    assert "kdive-drgn" not in leap_text


@pytest.mark.parametrize("distro", ["opensuse-tumbleweed", "opensuse-leap"])
def test_cloud_init_uses_the_predictable_suse_interface_name(tmp_path: Path, distro: str) -> None:
    cfg = baked_contents(_steps(tmp_path, distro))[KDIVE_CLOUD_CFG_PATH]
    assert "    eth0:\n" in cfg
    assert "    kdive-dhcp:\n" not in cfg


@pytest.mark.parametrize("distro", ["opensuse-tumbleweed", "opensuse-leap"])
def test_readiness_waits_for_cloud_init_and_sshd(tmp_path: Path, distro: str) -> None:
    steps = _steps(tmp_path, distro)
    dropin = baked_contents(steps)[_READINESS_DROPIN_PATH]
    assert "After=cloud-final.service sshd.service" in dropin
    assert "Requires=cloud-final.service sshd.service" in dropin
    assert "Wants=cloud-final.service sshd.service" not in dropin
    mkdir_index = steps.index(Mkdir(_READINESS_DROPIN_DIR))
    stage_index = next(
        index
        for index, step in enumerate(steps)
        if isinstance(step, StageFile) and step.path == _READINESS_DROPIN_PATH
    )
    assert mkdir_index < stage_index


def test_kdump_sysconfig_uses_one_no_argument_helper_and_required_programs(tmp_path: Path) -> None:
    text = rendered(_steps(tmp_path))
    assert f'KDUMP_POSTSCRIPT="{_POST_PATH}"' in text
    assert f"{_POST_PATH} $DIR" not in text
    assert "KDUMP_REQUIRED_PROGRAMS" in text
    for program in (
        _POST_PATH,
        "/usr/bin/mv",
        "/usr/bin/sync",
        "/usr/bin/umount",
        "/usr/sbin/poweroff",
    ):
        assert program in text


def _write_fake_command(directory: Path, name: str, log: Path) -> None:
    command = directory / name
    command.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> {log}\n", encoding="utf-8")
    command.chmod(0o755)


def _run_postscript(tmp_path: Path, invocation: str) -> Path:
    contents = baked_contents(_steps(tmp_path))[_POST_PATH]
    root = tmp_path / "crash"
    script = tmp_path / "kdive-suse-kdump-post"
    contents = contents.replace(_DUMP_ROOT, str(root))
    for path in ("/usr/bin/mv", "/usr/bin/sync", "/usr/bin/umount", "/usr/sbin/poweroff"):
        contents = contents.replace(path, Path(path).name)
    script.write_text(contents, encoding="utf-8")
    script.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    for name in ("sync", "umount", "poweroff"):
        _write_fake_command(fake_bin, name, log)
    env = {
        "PATH": f"{fake_bin}:{os.defpath}",
        "KDUMP_POSTSCRIPT": str(script),
    }
    command = "${KDUMP_POSTSCRIPT}" if invocation == "direct" else 'eval "${KDUMP_POSTSCRIPT}"'
    subprocess.run(["/bin/sh", "-c", command], check=True, env=env)
    assert log.read_text(encoding="utf-8").splitlines() == ["sync", "umount", "poweroff"]
    return root


@pytest.mark.parametrize("invocation", ["direct", "eval"])
@pytest.mark.parametrize("successful", [True, False])
def test_postscript_runs_under_both_kdump_forms(
    tmp_path: Path, invocation: str, successful: bool
) -> None:
    dump = tmp_path / "crash" / "current"
    dump.mkdir(parents=True)
    (dump / "vmcore").write_text("partial", encoding="utf-8")
    status = "vmcore status: saved successfully" if successful else "vmcore status: failed"
    (dump / "README.txt").write_text(f"header\n{status}\n", encoding="utf-8")

    _run_postscript(tmp_path, invocation)

    assert (dump / "vmcore").exists() is successful
    assert (dump / "vmcore-incomplete").exists() is not successful


def test_postscript_fails_closed_for_ambiguous_dump_directories(tmp_path: Path) -> None:
    for name in ("older", "current"):
        dump = tmp_path / "crash" / name
        dump.mkdir(parents=True)
        (dump / "vmcore").write_text(name, encoding="utf-8")
        (dump / "README.txt").write_text("vmcore status: saved successfully\n", encoding="utf-8")

    root = _run_postscript(tmp_path, "direct")

    assert not list(root.glob("*/vmcore"))
    assert len(list(root.glob("*/vmcore-incomplete"))) == 2


def test_postscript_handles_no_core(tmp_path: Path) -> None:
    root = _run_postscript(tmp_path, "direct")
    assert not list(root.glob("*/vmcore*"))


def test_normalize_writes_fstab_and_removes_crypttab(tmp_path: Path) -> None:
    scripts: list[str] = []
    rewrites: list[Path] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> str:
        script = str(kwargs.get("input_text", ""))
        scripts.append(script)
        for line in script.splitlines():
            if line.startswith("download /boot/initrd "):
                Path(line.removeprefix("download /boot/initrd ")).write_bytes(b"initrd")
        return ""

    def _fake_rewrite(path: Path, entry: str) -> bool:
        assert path.read_bytes() == b"initrd"
        assert entry.endswith("/20-kiwi-repart-disk.sh")
        rewrites.append(path)
        path.write_bytes(b"normalized")
        return True

    SuseFamily().normalize(
        tmp_path / "image.qcow2",
        _run_guestfs=_fake_run_guestfs,
        _rewrite_initrd=_fake_rewrite,
    )
    assert len(scripts) == 2
    assert "/etc/fstab" in scripts[0]
    assert "rm-f /etc/crypttab" in scripts[0]
    assert "selinux" not in scripts[0].lower()
    assert "download /boot/initrd " in scripts[0]
    assert "upload " in scripts[1] and " /boot/initrd" in scripts[1]
    assert len(rewrites) == 1


def test_normalize_does_not_upload_an_unchanged_initrd(tmp_path: Path) -> None:
    scripts: list[str] = []

    def _fake_run_guestfs(argv: list[str], **kwargs: object) -> str:
        script = str(kwargs.get("input_text", ""))
        scripts.append(script)
        for line in script.splitlines():
            if line.startswith("download /boot/initrd "):
                Path(line.removeprefix("download /boot/initrd ")).write_bytes(b"initrd")
        return ""

    SuseFamily().normalize(
        tmp_path / "image.qcow2",
        _run_guestfs=_fake_run_guestfs,
        _rewrite_initrd=lambda _path, _entry: False,
    )

    assert len(scripts) == 1
    assert "download /boot/initrd " in scripts[0]


def test_installed_helper_commands_are_not_accidental_packages(tmp_path: Path) -> None:
    assert installed(_steps(tmp_path)) == list(_ctx(tmp_path).packages)
