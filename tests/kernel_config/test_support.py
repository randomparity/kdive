from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    ROOTFS_MOUNT,
    BuiltIn,
    Clause,
    FeatureRequirement,
    feature_requirement,
)
from kdive.kernel_config.support import (
    built_in_required_symbols,
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)
from tests.kernel_config.config_fixtures import all_builtin as _all_builtin

_CRASH = feature_requirement(CRASH_CAPTURE)
_FULL = frozenset(
    {
        "KEXEC_CORE",
        "KEXEC",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "VMCORE_INFO",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }
)


def test_kaslr_off_full_gate_set_is_supported():
    # RANDOMIZE_BASE absent but every gate_required clause met -> no unmet clauses (supported).
    assert unmet_clauses(KernelConfig(_FULL, _FULL), _CRASH) == ()


def test_kexec_or_group_satisfied_by_either_syscall():
    only_file = (_FULL - {"KEXEC"}) | {"KEXEC_FILE"}
    assert unmet_clauses(KernelConfig(frozenset(only_file), frozenset(only_file)), _CRASH) == ()


def test_missing_one_clause_is_unsupported_and_named():
    cfg = _all_builtin(_FULL - {"PROC_VMCORE"})
    unmet = unmet_clauses(cfg, _CRASH)
    assert unmet != ()
    assert missing_symbols(unmet) == ["PROC_VMCORE"]


def test_missing_both_kexec_syscalls_names_both():
    cfg = _all_builtin(_FULL - {"KEXEC"})  # neither KEXEC nor KEXEC_FILE
    unmet = unmet_clauses(cfg, _CRASH)
    assert missing_symbols(unmet) == ["KEXEC", "KEXEC_FILE"]


_ROOTFS = feature_requirement(ROOTFS_MOUNT)


def test_advertised_clauses_read_the_advertise_set_not_the_empty_gate():
    # rootfs_mount has no gate_required, so unmet_clauses is always empty; the advisory path must
    # read the advertised set instead. BTRFS_FS keeps the config non-degenerate while enabling no
    # root filesystem kdive boots (#1626 made XFS_FS one, so it no longer serves that role here).
    cfg = _all_builtin({"BTRFS_FS"})
    assert unmet_clauses(cfg, _ROOTFS) == ()
    assert missing_symbols(unmet_advertised_clauses(cfg, _ROOTFS)) == [
        "EXT4_FS",
        "VIRTIO_BLK",
        "XFS_FS",
    ]


def test_advertised_clauses_satisfied_by_full_boot_set():
    cfg = _all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    assert unmet_advertised_clauses(cfg, _ROOTFS) == ()


def test_advertised_root_filesystem_or_group_accepts_either_family():
    # #1626: {EXT4_FS, XFS_FS} is one OR-group, so an XFS-root guest's kernel is satisfied without
    # ext4 and vice versa — the AND-of-OR semantics are what make both cases silent.
    xfs_root = _all_builtin({"XFS_FS", "VIRTIO_BLK"})
    ext4_root = _all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    assert unmet_advertised_clauses(xfs_root, _ROOTFS) == ()
    assert unmet_advertised_clauses(ext4_root, _ROOTFS) == ()


# The three built-in values of #1860, exercised on synthetic features rather than on the live
# roster, so these stay pinned to the *semantics* while the registry-content assertions in
# test_requirements.py stay pinned to which real clause carries which value.
_MODULAR = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset())
_BUILT_IN = _all_builtin({"EXT4_FS", "VIRTIO_BLK"})


def _synthetic(built_in: BuiltIn) -> FeatureRequirement:
    return FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the built-in requirement",
        advertised=(Clause(frozenset({"EXT4_FS"}), built_in),),
    )


def test_not_required_is_the_default_and_a_modular_symbol_satisfies_it():
    # The reason #1860 did not narrow the _ENABLED regex: for KASAN, ftrace, kcov and BPF symbols
    # a module IS the feature, so an unmarked clause must keep accepting =m.
    assert Clause(frozenset({"KASAN"})).built_in is BuiltIn.NOT_REQUIRED
    feature = _synthetic(BuiltIn.NOT_REQUIRED)
    assert unmet_advertised_clauses(_MODULAR, feature) == ()
    assert unmet_advertised_clauses(_BUILT_IN, feature) == ()


def test_required_rejects_a_modular_symbol_whatever_the_build_uploaded():
    # SERIAL_8250 and IKCONFIG: the Kconfig-level =y, which an initrd does not relieve.
    feature = _synthetic(BuiltIn.REQUIRED)
    assert missing_symbols(unmet_advertised_clauses(_MODULAR, feature)) == ["EXT4_FS"]
    assert missing_symbols(unmet_advertised_clauses(_MODULAR, feature, has_initrd=True)) == [
        "EXT4_FS"
    ]
    assert unmet_advertised_clauses(_BUILT_IN, feature, has_initrd=False) == ()


def test_unless_initrd_rejects_a_modular_symbol_only_when_no_initrd_was_uploaded():
    # The boot-ordering =y: nothing loads a module before root is mounted, unless the build
    # uploaded an initrd to load it from. This is the half REQUIRED must not be collapsed into.
    feature = _synthetic(BuiltIn.UNLESS_INITRD)
    assert missing_symbols(unmet_advertised_clauses(_MODULAR, feature)) == ["EXT4_FS"]
    assert unmet_advertised_clauses(_MODULAR, feature, has_initrd=True) == ()
    assert unmet_advertised_clauses(_BUILT_IN, feature, has_initrd=False) == ()


def test_the_strict_reading_is_the_default_so_a_forgetful_seam_over_warns():
    # ADR-0330's direction for this advisory: a caller that omits the fact gets the answer that
    # warns, not the one that falls silent.
    feature = _synthetic(BuiltIn.UNLESS_INITRD)
    assert unmet_advertised_clauses(_MODULAR, feature) != ()


def test_the_built_in_requirement_reaches_the_refusal_set_and_not_only_the_advertised_one():
    # unmet_clauses and unmet_advertised_clauses read different fields; both must apply the value,
    # or a future gated clause would refuse on presence alone.
    gated = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the built-in requirement",
        advertised=(Clause(frozenset({"EXT4_FS"}), BuiltIn.UNLESS_INITRD),),
        gate_required=(Clause(frozenset({"EXT4_FS"}), BuiltIn.UNLESS_INITRD),),
    )
    assert missing_symbols(unmet_clauses(_MODULAR, gated)) == ["EXT4_FS"]
    assert unmet_clauses(_MODULAR, gated, has_initrd=True) == ()


def test_an_or_group_is_satisfied_by_whichever_member_is_built_in():
    # The clause value applies per clause, not per symbol: a kernel with XFS modular and ext4
    # built in mounts its root, so the OR-group is met.
    feature = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the built-in requirement",
        advertised=(Clause(frozenset({"EXT4_FS", "XFS_FS"}), BuiltIn.UNLESS_INITRD),),
    )
    mixed = KernelConfig(frozenset({"EXT4_FS", "XFS_FS"}), frozenset({"EXT4_FS"}))
    assert unmet_advertised_clauses(mixed, feature) == ()
    modular_only = KernelConfig(frozenset({"EXT4_FS", "XFS_FS"}), frozenset())
    assert missing_symbols(unmet_advertised_clauses(modular_only, feature)) == [
        "EXT4_FS",
        "XFS_FS",
    ]


def test_built_in_required_names_the_modular_half_of_missing_and_nothing_else():
    # #1860's payload key: the subset of `missing` the config enables as =m, which is what lets an
    # agent tell "you do not have this" from "you have this in a form that cannot load in time".
    # VIRTIO_BLK is modular; XFS_FS/EXT4_FS are absent entirely.
    feature = feature_requirement(ROOTFS_MOUNT)
    cfg = KernelConfig(frozenset({"VIRTIO_BLK"}), frozenset())
    unmet = unmet_advertised_clauses(cfg, feature)
    assert missing_symbols(unmet) == ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]
    assert built_in_required_symbols(cfg, unmet) == ["VIRTIO_BLK"]


def test_built_in_required_is_empty_when_every_missing_symbol_is_absent_outright():
    feature = feature_requirement(ROOTFS_MOUNT)
    cfg = _all_builtin({"BTRFS_FS"})
    unmet = unmet_advertised_clauses(cfg, feature)
    assert missing_symbols(unmet) == ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]
    assert built_in_required_symbols(cfg, unmet) == []
