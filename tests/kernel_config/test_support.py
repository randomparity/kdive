from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import CRASH_CAPTURE, ROOTFS_MOUNT, feature_requirement
from kdive.kernel_config.support import (
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)

_CRASH = feature_requirement(CRASH_CAPTURE)
# Exactly the symbols crash_capture gates, and no others: #1854 dropped the derived KEXEC_CORE
# and VMCORE_INFO out of the refusal set, so listing them here would leave a fixture claiming to
# be the full gate set while naming two symbols the gate no longer reads.
_FULL = frozenset(
    {
        "KEXEC",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }
)


def test_kaslr_off_full_gate_set_is_supported():
    # RANDOMIZE_BASE absent but every gate_required clause met -> no unmet clauses (supported).
    assert unmet_clauses(KernelConfig(_FULL), _CRASH) == ()


def test_kexec_or_group_satisfied_by_either_syscall():
    only_file = (_FULL - {"KEXEC"}) | {"KEXEC_FILE"}
    assert unmet_clauses(KernelConfig(frozenset(only_file)), _CRASH) == ()


def test_missing_one_clause_is_unsupported_and_named():
    cfg = KernelConfig(_FULL - {"PROC_VMCORE"})
    unmet = unmet_clauses(cfg, _CRASH)
    assert unmet != ()
    assert missing_symbols(unmet) == ["PROC_VMCORE"]


def test_missing_both_kexec_syscalls_names_both():
    cfg = KernelConfig(_FULL - {"KEXEC"})  # neither KEXEC nor KEXEC_FILE
    unmet = unmet_clauses(cfg, _CRASH)
    assert missing_symbols(unmet) == ["KEXEC", "KEXEC_FILE"]


_ROOTFS = feature_requirement(ROOTFS_MOUNT)


def test_advertised_clauses_read_the_advertise_set_not_the_empty_gate():
    # rootfs_mount has no gate_required, so unmet_clauses is always empty; the advisory path must
    # read the advertised set instead. BTRFS_FS keeps the config non-degenerate while enabling no
    # root filesystem kdive boots (#1626 made XFS_FS one, so it no longer serves that role here).
    cfg = KernelConfig(frozenset({"BTRFS_FS"}))
    assert unmet_clauses(cfg, _ROOTFS) == ()
    assert missing_symbols(unmet_advertised_clauses(cfg, _ROOTFS)) == [
        "EXT4_FS",
        "VIRTIO_BLK",
        "XFS_FS",
    ]


def test_advertised_clauses_satisfied_by_full_boot_set():
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    assert unmet_advertised_clauses(cfg, _ROOTFS) == ()


def test_advertised_root_filesystem_or_group_accepts_either_family():
    # #1626: {EXT4_FS, XFS_FS} is one OR-group, so an XFS-root guest's kernel is satisfied without
    # ext4 and vice versa — the AND-of-OR semantics are what make both cases silent.
    xfs_root = KernelConfig(frozenset({"XFS_FS", "VIRTIO_BLK"}))
    ext4_root = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    assert unmet_advertised_clauses(xfs_root, _ROOTFS) == ()
    assert unmet_advertised_clauses(ext4_root, _ROOTFS) == ()
