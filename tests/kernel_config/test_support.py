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
    assert unmet_clauses(KernelConfig(_FULL, _FULL), _CRASH, arch=_X86) == ()


def test_kexec_or_group_satisfied_by_either_syscall():
    only_file = (_FULL - {"KEXEC"}) | {"KEXEC_FILE"}
    cfg = KernelConfig(frozenset(only_file), frozenset(only_file))
    assert unmet_clauses(cfg, _CRASH, arch=_X86) == ()


def test_missing_one_clause_is_unsupported_and_named():
    cfg = _all_builtin(_FULL - {"PROC_VMCORE"})
    unmet = unmet_clauses(cfg, _CRASH, arch=_X86)
    assert unmet != ()
    assert missing_symbols(unmet) == ["PROC_VMCORE"]


def test_missing_both_kexec_syscalls_names_both():
    cfg = _all_builtin(_FULL - {"KEXEC"})  # neither KEXEC nor KEXEC_FILE
    unmet = unmet_clauses(cfg, _CRASH, arch=_X86)
    assert missing_symbols(unmet) == ["KEXEC", "KEXEC_FILE"]


_ROOTFS = feature_requirement(ROOTFS_MOUNT)


def test_advertised_clauses_read_the_advertise_set_not_the_empty_gate():
    # rootfs_mount has no gate_required, so unmet_clauses is always empty; the advisory path must
    # read the advertised set instead. BTRFS_FS keeps the config non-degenerate while enabling no
    # root filesystem kdive boots (#1626 made XFS_FS one, so it no longer serves that role here).
    cfg = _all_builtin({"BTRFS_FS"})
    assert unmet_clauses(cfg, _ROOTFS, arch=None) == ()
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


def test_unless_initrd_rejects_a_modular_symbol_only_when_nothing_can_load_it_in_time():
    # The boot-ordering =y: nothing loads a module before root is mounted, unless something can.
    # Two facts answer that and EITHER alone relieves the clause (ADR-0545) - an uploaded initrd
    # artifact, or a target whose guest builds its own initramfs. This is the half REQUIRED must
    # not be collapsed into. The last arm is what stops an AND from passing the three above.
    feature = _synthetic(BuiltIn.UNLESS_INITRD)
    assert missing_symbols(unmet_advertised_clauses(_MODULAR, feature)) == ["EXT4_FS"]
    assert unmet_advertised_clauses(_MODULAR, feature, has_initrd=True) == ()
    assert unmet_advertised_clauses(_MODULAR, feature, guest_builds_initramfs=True) == ()
    assert unmet_advertised_clauses(_BUILT_IN, feature, has_initrd=False) == ()
    assert (
        unmet_advertised_clauses(_MODULAR, feature, has_initrd=True, guest_builds_initramfs=False)
        == ()
    )


def test_the_strict_reading_is_the_default_on_both_axes_so_a_forgetful_seam_over_warns():
    # ADR-0330's direction for this advisory: a caller that omits a fact gets the answer that
    # warns, not the one that falls silent. One arm per keyword, each omitting exactly that one,
    # so a default flipped on either axis alone is reported rather than masked by the other.
    feature = _synthetic(BuiltIn.UNLESS_INITRD)
    assert unmet_advertised_clauses(_MODULAR, feature) != ()
    assert unmet_advertised_clauses(_MODULAR, feature, has_initrd=False) != ()
    assert unmet_advertised_clauses(_MODULAR, feature, guest_builds_initramfs=False) != ()


def test_the_built_in_requirement_reaches_the_refusal_set_and_not_only_the_advertised_one():
    # unmet_clauses and unmet_advertised_clauses read different fields; both must apply the value,
    # or a future gated clause would refuse on presence alone.
    gated = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the built-in requirement",
        advertised=(Clause(frozenset({"EXT4_FS"}), BuiltIn.UNLESS_INITRD),),
        gate_required=(Clause(frozenset({"EXT4_FS"}), BuiltIn.UNLESS_INITRD),),
    )
    assert missing_symbols(unmet_clauses(_MODULAR, gated, arch=None)) == ["EXT4_FS"]
    assert unmet_clauses(_MODULAR, gated, arch=None, has_initrd=True) == ()


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


# The arch scope of ADR-0544 §3, exercised on synthetic features for the same reason the built-in
# values above are: these stay pinned to the *semantics* of skipping, while the registry-content
# assertions in test_requirements.py stay pinned to which real clause carries which arch.
_X86 = "x86_64"
_PPC = "ppc64le"


def _arch_scoped(arches: frozenset[str] | None) -> FeatureRequirement:
    return FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the arch scope",
        advertised=(Clause(frozenset({"HVC_CONSOLE"}), arches=arches),),
    )


def test_a_clause_scoped_to_another_arch_is_skipped():
    # ADR-0544 §3. HVC_CONSOLE is the pseries console and does not exist as a requirement on x86,
    # so an x86 kernel without it must not be reported as missing anything.
    feature = _arch_scoped(frozenset({_PPC}))
    assert unmet_advertised_clauses(_all_builtin({"EXT4_FS"}), feature, arch=_X86) == ()


def test_a_clause_scoped_to_the_supplied_arch_is_evaluated_in_both_directions():
    # Non-vacuity for the skip above: on the arch it IS scoped to, the same clause still decides.
    feature = _arch_scoped(frozenset({_PPC}))
    bare = _all_builtin({"EXT4_FS"})
    assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=_PPC)) == ["HVC_CONSOLE"]
    complete = _all_builtin({"HVC_CONSOLE"})
    assert unmet_advertised_clauses(complete, feature, arch=_PPC) == ()


def test_a_scoped_clause_is_skipped_when_the_arch_is_unknown():
    # ADR-0544 §3: "skip a clause scoped ... at all when the arch is unknown - never inventing a
    # requirement kdive cannot establish". The default is the unknown arch, so a seam that holds
    # no arch cannot fault a kernel for a symbol that may not apply to it. This is the OPPOSITE
    # direction from has_initrd's strict default, and deliberately so: an omitted initrd fact
    # over-warns about a symbol that is certainly required, while an omitted arch would invent a
    # requirement that may not exist at all.
    feature = _arch_scoped(frozenset({_PPC}))
    bare = _all_builtin({"EXT4_FS"})
    assert unmet_advertised_clauses(bare, feature, arch=None) == ()
    assert unmet_advertised_clauses(bare, feature) == ()


def test_an_unscoped_clause_is_evaluated_on_every_arch_and_on_no_arch():
    # None means every arch, so the skip must never reach a clause that did not opt in - which is
    # every clause in the registry but the three serial_console ones.
    feature = _arch_scoped(None)
    bare = _all_builtin({"EXT4_FS"})
    for arch in (_X86, _PPC, None):
        assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=arch)) == [
            "HVC_CONSOLE"
        ], arch


def test_a_multi_arch_scope_is_evaluated_on_each_member():
    # `arches` is a set, not a single value: a clause naming two arches applies on both.
    feature = _arch_scoped(frozenset({_X86, _PPC}))
    bare = _all_builtin({"EXT4_FS"})
    assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=_X86)) == ["HVC_CONSOLE"]
    assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=_PPC)) == ["HVC_CONSOLE"]
    assert unmet_advertised_clauses(bare, feature, arch="s390x") == ()


def test_the_arch_scope_reaches_the_refusal_set_and_not_only_the_advertised_one():
    # unmet_clauses and unmet_advertised_clauses read different fields; both must apply the scope,
    # or an arch-scoped gate clause would refuse a kernel of the wrong arch. crash_capture's
    # {FW_CFG_SYSFS} is now exactly that (#1875), so this synthetic fixture is no longer the only
    # thing holding the pair together - it is what keeps them from diverging on a feature the
    # registry does not happen to carry.
    gated = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the arch scope",
        advertised=(Clause(frozenset({"HVC_CONSOLE"}), arches=frozenset({_PPC})),),
        gate_required=(Clause(frozenset({"HVC_CONSOLE"}), arches=frozenset({_PPC})),),
    )
    bare = _all_builtin({"EXT4_FS"})
    assert missing_symbols(unmet_clauses(bare, gated, arch=_PPC)) == ["HVC_CONSOLE"]
    assert unmet_clauses(bare, gated, arch=_X86) == ()
    assert unmet_clauses(bare, gated, arch=None) == ()


def test_the_arch_scope_and_the_built_in_requirement_compose():
    # The two axes are independent: an in-scope clause still applies its built-in requirement, and
    # an out-of-scope one is skipped whatever that requirement says. Without this, adding the
    # second axis could have made either one shadow the other.
    feature = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the arch scope",
        advertised=(
            Clause(frozenset({"SERIAL_8250"}), BuiltIn.REQUIRED, arches=frozenset({_X86})),
        ),
    )
    modular = KernelConfig(frozenset({"SERIAL_8250"}), frozenset())
    assert missing_symbols(unmet_advertised_clauses(modular, feature, arch=_X86)) == ["SERIAL_8250"]
    assert unmet_advertised_clauses(modular, feature, arch=_PPC) == ()
    built_in = _all_builtin({"SERIAL_8250"})
    assert unmet_advertised_clauses(built_in, feature, arch=_X86) == ()
