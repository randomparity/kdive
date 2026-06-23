"""Unit tests for the build-config seed (ADR-0096).

The seed's publish / idempotency / source-aware behavior is exercised DB-backed in
``test_seed_db.py``: the source-guarded seed upsert acquires a real ``pg_advisory_xact_lock``
(ADR-0119) that a connection double cannot satisfy, so the former fake-conn seed tests were
retired in favor of the real-connection ones. This module keeps only the connection-free
packaged-fragment check.
"""

from __future__ import annotations

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH


def test_kdump_fragment_is_packaged_and_nonempty() -> None:
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert data.strip()
    assert b"CONFIG_CRASH_DUMP=y" in data


def test_kdump_fragment_carries_xfs_root_support() -> None:
    # The remote base image root is XFS V5; x86_64_defconfig has EXT4 but not XFS, so without these
    # the built kernel cannot mount root and boots to emergency mode (ADR-0183, #587).
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert b"CONFIG_XFS_FS=y" in data
    assert b"CONFIG_XFS_POSIX_ACL=y" in data


def test_kdump_fragment_carries_host_dump_vmcoreinfo_symbols() -> None:
    # A parseable host_dump core needs the guest kernel to write its VMCOREINFO note to
    # QEMU's etc/vmcoreinfo fw_cfg entry at boot (#708). That write path lives behind
    # CONFIG_VMCORE_INFO and is performed by the qemu_fw_cfg driver, which must be built
    # in (=y) to probe before a host-side dump. VMCORE_INFO is a built-in-only requirement;
    # the =y survival guard keeps FW_CFG_SYSFS from silently building as a module.
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert b"CONFIG_VMCORE_INFO=y" in data
    assert b"CONFIG_FW_CFG_SYSFS=y" in data


def test_kdump_fragment_exposes_effective_config_for_diagnosis() -> None:
    # IKCONFIG embeds the built kernel's effective .config and IKCONFIG_PROC exposes it at
    # /proc/config.gz, so a live drive can confirm whether FW_CFG_SYSFS/VMCORE_INFO resolved
    # =y vs =m and whether the qemu_fw_cfg driver probed (#708).
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert b"CONFIG_IKCONFIG=y" in data
    assert b"CONFIG_IKCONFIG_PROC=y" in data


def test_kdump_fragment_carries_in_guest_arming_prerequisites() -> None:
    # Fedora kdumpctl builds a zstd-squashfs crash initramfs and loads the crash kernel via the
    # kexec_file_load syscall; without these the from-source kernel cannot arm kdump (ADR-0213,
    # #688). SQUASHFS/SQUASHFS_ZSTD + the loop/overlay backing are dracut's squash module;
    # KEXEC_FILE is `kexec -s -p`. =y so the crash environment loads no extra modules first.
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert b"CONFIG_SQUASHFS=y" in data
    assert b"CONFIG_SQUASHFS_ZSTD=y" in data
    assert b"CONFIG_BLK_DEV_LOOP=y" in data
    assert b"CONFIG_OVERLAY_FS=y" in data
    assert b"CONFIG_KEXEC_FILE=y" in data
