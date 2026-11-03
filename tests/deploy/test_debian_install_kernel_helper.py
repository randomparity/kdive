"""Execute the Debian helper with only filesystem roots and native tool boundaries isolated."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

HELPER = (
    Path(__file__).resolve().parents[2]
    / "deploy/remote-libvirt-guest-helpers/debian/kdive-install-kernel"
)
RELEASE = "6.18.54-kdive-test"


@pytest.fixture
def guest(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    roots = {
        "BOOT_DIR": tmp_path / "boot/kdive",
        "MODULES_DIR": tmp_path / "modules",
        "GRUB_SCRIPT": tmp_path / "grub.d/42_kdive",
        "GRUB_LIB": tmp_path / "grub-lib",
        "DEVICE_LINKS": tmp_path / "dev/disk",
        "KDUMP_DEFAULTS": tmp_path / "kdump-tools",
    }
    source = HELPER.read_text()
    for name, path in roots.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace only fixed filesystem boundary assignments, never helper logic.
        old = next(line for line in source.splitlines() if line.startswith(name + "="))
        source = source.replace(old + "\n", f"{name}={shlex.quote(str(path))}\n", 1)
    helper = tmp_path / "helper"
    helper.write_text(source)
    (tmp_path / "grub.d/sentinel").write_text("unrelated")
    roots["KDUMP_DEFAULTS"].write_text(
        "# operator comment\nUSE_KDUMP=0\nMAKEDUMP_ARGS='-c -d 31'\n"
    )
    roots["GRUB_LIB"].write_text(
        """# Native grub-mkconfig_lib reads unset optional variables without nounset.
if [ "x$grub_probe" = x ]; then grub_probe=unused; fi
prepare_grub_to_access_device() { printf 'search --fs-uuid --set=root boot-id\\n'; }
make_system_path_relative_to_its_root() {
  if [ "$1" = / ]; then printf '%s\\n' "${SUBVOL:-/}"; else printf '/kdive/%s\\n' "${1##*/}"; fi
}
uses_abstraction() { [ "${LVM:-0}" = 1 ]; }
"""
    )
    device = tmp_path / "device"
    device.touch()
    for kind, value in (("by-uuid", "root-id"), ("by-partuuid", "part-id")):
        directory = roots["DEVICE_LINKS"] / kind
        directory.mkdir(parents=True)
        (directory / value).symlink_to(device)
    bins = tmp_path / "bin"
    bins.mkdir()
    scripts = {
        "depmod": "exit 0",
        "update-initramfs": """printf '%s\\n' "$*" >>"$STATE/initramfs.log"
[ "${FAIL_INITRAMFS:-0}" = 0 ] || exit 2
printf initrd >"$5/initrd.img-$3" """,
        "update-grub": """[ "${FAIL_GRUB:-0}" = 0 ] || exit 3
bash "$STATE/grub.d/42_kdive" >"$STATE/grub.cfg" """,
        "systemctl": """printf '%s\\n' "$*" >>"$STATE/systemctl.log"
[ "${FAIL_SYSTEMCTL:-0}" = 0 ]""",
        "grub-reboot": """printf '%s\\n' "$*" >>"$STATE/reboot.log"
[ "${FAIL_SELECT:-0}" = 0 ]""",
        "systemd-run": """printf '%s\\n' "$*" >>"$STATE/detached.log" """,
    }
    for name, body in scripts.items():
        tool = bins / name
        tool.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        tool.chmod(0o755)
    bundle_tree = tmp_path / "bundle"
    (bundle_tree / "boot").mkdir(parents=True)
    (bundle_tree / "boot/vmlinuz").write_bytes(b"kernel")
    (bundle_tree / f"lib/modules/{RELEASE}").mkdir(parents=True)
    with tarfile.open(tmp_path / "kernel.tar.gz", "w:gz") as archive:
        archive.add(bundle_tree / "boot", arcname="boot")
        archive.add(bundle_tree / "lib", arcname="lib")
    env = {
        **os.environ,
        "PATH": f"{bins}:/usr/bin:/bin",
        "STATE": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "GRUB_DEVICE": str(device),
        "GRUB_DEVICE_BOOT": "separate-boot-device",
        "GRUB_DEVICE_UUID": "root-id",
        "GRUB_DEVICE_PARTUUID": "part-id",
        "GRUB_FS": "ext2",
        "GRUB_CMDLINE_LINUX": "quiet crashkernel=512M",
    }
    return helper, env


def run(
    guest: tuple[Path, dict[str, str]], *args: str, **changes: str
) -> subprocess.CompletedProcess[str]:
    helper, env = guest
    return subprocess.run(
        ["/bin/bash", str(helper), *args],
        env={**env, **changes},
        text=True,
        capture_output=True,
        check=False,
    )


def install(
    guest: tuple[Path, dict[str, str]],
    cmdline: str = "console=ttyS0",
    method: str = "gdbstub",
    **changes: str,
) -> subprocess.CompletedProcess[str]:
    helper, _ = guest
    return run(
        guest,
        "install",
        "--url",
        (helper.parent / "kernel.tar.gz").as_uri(),
        "--cmdline",
        cmdline,
        "--method",
        method,
        **changes,
    )


def test_repeat_install_preserves_default_and_uses_one_isolated_entry(
    guest: tuple[Path, dict[str, str]],
) -> None:
    helper, _ = guest
    root = helper.parent
    for _ in range(2):
        result = install(guest)
        assert result.returncode == 0, result.stderr
    assert (root / f"boot/kdive/vmlinuz-{RELEASE}").read_bytes() == b"kernel"
    assert not list((root / "boot").glob("vmlinuz-*"))
    assert (root / "grub.d/sentinel").read_text() == "unrelated"
    assert (root / "grub.cfg").read_text().count("menuentry ") == 1
    calls = (root / "initramfs.log").read_text().splitlines()
    assert calls[0].startswith("-c ") and calls[1].startswith("-u ")
    assert not (root / "reboot.log").exists()
    assert "USE_KDUMP=0" in (root / "kdump-tools").read_text()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "UUID=root-id"),
        (
            {"GRUB_DISABLE_LINUX_UUID": "true", "GRUB_DISABLE_LINUX_PARTUUID": "false"},
            "PARTUUID=part-id",
        ),
        ({"GRUB_DISABLE_LINUX_UUID": "true"}, "device"),
        ({"GRUB_DEVICE_UUID": "", "GRUB_DEVICE_PARTUUID": ""}, "device"),
        ({"GRUB_DEVICE_UUID": "missing", "GRUB_DEVICE_PARTUUID": "missing"}, "device"),
        ({"LVM": "1"}, "device"),
    ],
)
def test_native_root_selection(
    guest: tuple[Path, dict[str, str]], changes: dict[str, str], expected: str
) -> None:
    helper, _ = guest
    result = install(guest, **changes)
    assert result.returncode == 0, result.stderr
    root = str(helper.parent / "device") if expected == "device" else expected
    config = (helper.parent / "grub.cfg").read_text()
    assert shlex.quote("root=" + root).strip("'") in config
    assert "search --fs-uuid --set=root boot-id" in config
    assert f"'/kdive/vmlinuz-{RELEASE}'" in config


def test_btrfs_and_requested_words_survive_without_execution(
    guest: tuple[Path, dict[str, str]],
) -> None:
    helper, _ = guest
    marker = helper.parent / "injected"
    cmdline = f"""console=ttyS0 "quoted=a b" "quote=a'b" 'literal=$(touch {marker});$x'"""
    result = install(guest, cmdline, GRUB_FS="btrfs", SUBVOL="/@root")
    assert result.returncode == 0, result.stderr
    config = (helper.parent / "grub.cfg").read_text()
    assert "'rootflags=subvol=@root'" in config
    assert "'quoted=a b'" in config
    assert "'quote=a'\\''b'" in config
    assert "crashkernel" not in config
    assert not marker.exists()
    checker = shutil.which("grub-script-check") or shutil.which("grub2-script-check")
    if checker:
        subprocess.run([checker, str(helper.parent / "grub.cfg")], check=True, capture_output=True)


def test_kdump_updates_only_its_three_values(guest: tuple[Path, dict[str, str]]) -> None:
    helper, _ = guest
    for _ in range(2):
        result = install(guest, "console=ttyS0 crashkernel=256M", "kdump")
        assert result.returncode == 0, result.stderr
    config = (helper.parent / "kdump-tools").read_text()
    assert "# operator comment" in config and "MAKEDUMP_ARGS='-c -d 31'" in config
    assert config.count("USE_KDUMP=1") == 1
    assert f"KDUMP_KERNEL={helper.parent}/boot/kdive/vmlinuz-{RELEASE}" in config
    assert f"KDUMP_INITRD={helper.parent}/boot/kdive/initrd.img-{RELEASE}" in config
    assert "crashkernel=256M" in (helper.parent / "grub.cfg").read_text()
    assert (helper.parent / "systemctl.log").read_text().splitlines() == [
        "enable kdump-tools.service",
        "enable kdump-tools.service",
    ]


@pytest.mark.parametrize("failure", ["FAIL_INITRAMFS", "FAIL_GRUB", "FAIL_SYSTEMCTL"])
def test_native_install_failures_are_permanent(
    guest: tuple[Path, dict[str, str]], failure: str
) -> None:
    result = install(guest, method="kdump", **{failure: "1"})
    assert result.returncode == 1
    assert not (guest[0].parent / "detached.log").exists()


def test_boot_selects_before_detaching_and_rejects_selection_failure(
    guest: tuple[Path, dict[str, str]],
) -> None:
    helper, _ = guest
    result = run(guest, "boot", FAIL_SELECT="1")
    assert result.returncode == 1
    assert not (helper.parent / "detached.log").exists()
    result = run(guest, "boot")
    assert result.returncode == 0, result.stderr
    assert (helper.parent / "reboot.log").read_text().splitlines() == ["kdive", "kdive"]
    assert "--on-active=2 systemctl reboot" in (helper.parent / "detached.log").read_text()


def test_status_and_identity_keep_the_wire_shape(guest: tuple[Path, dict[str, str]]) -> None:
    from uuid import UUID

    identity = run(guest, "boot-id")
    assert identity.returncode == 0
    UUID(identity.stdout.strip())
    result = run(guest, "kdump-status")
    assert result.returncode == 0
    values = dict(line.split("=") for line in result.stdout.splitlines())
    assert set(values) == {"crash_size", "crash_loaded"}
    assert all(int(value) >= 0 for value in values.values())


@pytest.mark.parametrize(
    "args", [["wat"], ["install", "--url"], ["install", "--url", "file:///missing"]]
)
def test_invalid_command_is_permanent(guest: tuple[Path, dict[str, str]], args: list[str]) -> None:
    assert run(guest, *args).returncode == 1


def test_fetch_failure_alone_is_transient(guest: tuple[Path, dict[str, str]]) -> None:
    result = run(
        guest, "install", "--url", "file:///nonexistent-kdive-bundle", "--cmdline", "quiet"
    )
    assert result.returncode == 75
    (guest[0].parent / "kernel.tar.gz").write_bytes(b"not a tar")
    assert install(guest).returncode == 1


@pytest.mark.parametrize("curl_exit,expected", [(126, 1), (127, 1), (7, 75)])
def test_unexecutable_curl_is_not_a_fetch_failure(
    guest: tuple[Path, dict[str, str]], curl_exit: int, expected: int
) -> None:
    curl = guest[0].parent / "bin/curl"
    curl.write_text(f"#!/bin/bash\nexit {curl_exit}\n")
    curl.chmod(0o755)
    assert install(guest).returncode == expected


def test_missing_curl_is_permanent(guest: tuple[Path, dict[str, str]]) -> None:
    empty = guest[0].parent / "empty-bin"
    empty.mkdir()
    result = install(guest, PATH=str(empty))
    assert result.returncode == 1
    assert "curl is not installed" in result.stderr
