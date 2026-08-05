from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    CRASH_CAPTURE_RHEL_GUEST,
    FEATURE_REQUIREMENTS,
    SYSRQ,
    feature_manifest,
    feature_requirement,
)
from kdive.kernel_config.support import missing_symbols, unmet_advertised_clauses


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause}
    assert frozenset({"KEXEC", "KEXEC_FILE"}) in feat.gate_required  # either load syscall
    assert feat.gated is True


def test_only_the_named_gate_consumers_are_gated_and_the_rest_advertise_only():
    # #1848: this used to iterate six literal ids, so an advertise-only feature added later was
    # silently uncovered. Deriving both sets from the roster fixes that in both directions - a
    # new advertise-only feature is checked without editing the test, and a new *gated* feature
    # fails here on purpose. gate.py imports exactly CRASH_CAPTURE and ROOTFS_MOUNT and the only
    # refusal seams are crash-capture arming and sysrq, so growing the gated set is a decision,
    # not a data addition.
    gated = {f.feature for f in FEATURE_REQUIREMENTS if f.gated}
    assert gated == {CRASH_CAPTURE, SYSRQ}

    advertise_only = [f for f in FEATURE_REQUIREMENTS if f.feature not in gated]
    assert len(advertise_only) == len(FEATURE_REQUIREMENTS) - 2
    for feat in advertise_only:
        assert feat.gate_required == (), feat.feature
        assert feat.gated is False, feat.feature
        # an advertise-only feature that advertises nothing would pass the two asserts above
        # while telling the agent nothing at all
        assert feat.advertised, feat.feature
        assert feat.summary.strip(), feat.feature


def test_sysrq_is_advertised_and_gate_required_magic_sysrq():
    feat = feature_requirement(SYSRQ)
    assert feat.gate_required == (frozenset({"MAGIC_SYSRQ"}),)


def test_manifest_covers_every_feature_and_exposes_advertised_not_gate_required():
    import json

    manifest = feature_manifest()
    assert {m["feature"] for m in manifest} == {f.feature for f in FEATURE_REQUIREMENTS}
    entry = next(m for m in manifest if m["feature"] == CRASH_CAPTURE)
    assert entry["gated"] is True
    assert entry["summary"]
    assert isinstance(entry["requirements"], list)
    # advertised superset carries KASLR (advertise-only); the gate-set exclusion is asserted above
    assert "RANDOMIZE_BASE" in json.dumps(entry["requirements"])
    assert "gate_required" not in entry  # internal, not advertised


def test_debuginfo_summary_names_use_case_and_cost():
    # #1350: the advice must steer an agent away from DWARF5 for a console-log-only
    # investigation by naming *when* debuginfo is useful and *what* it costs. A bare
    # "resolve symbols" summary gave no basis to omit it, so an agent enabled DWARF5 on a
    # boot-time panic reproducer and inflated the module tree to ~2 GB.
    summary = feature_requirement("debuginfo").summary.lower()
    # use case: live introspection or offline vmcore analysis
    assert "drgn" in summary or "vmcore" in summary
    # cost: DWARF tables in every module, large module-tree growth
    assert ".ko" in summary
    assert "10-50x" in summary or "module tree" in summary
    # explicit omit-guidance for the wasteful case
    assert "omit" in summary


def test_unknown_feature_raises():
    import pytest

    with pytest.raises(KeyError):
        feature_requirement("does_not_exist")


def test_rootfs_mount_matches_the_real_direct_kernel_boot():
    # #1094: rootfs_mount used to advertise a squashfs+overlay boot path that does not exist
    # anywhere in the tree. Those stay out — the kdive-provisioned boot (ADR-0030) is a whole-disk
    # qcow2 mounted direct-kernel via root=/dev/vda (a virtio-blk device) with no initramfs.
    # #1626 refines the filesystem half only: a remote or agent-uploaded rootfs (ADR-0183/0440/
    # 0441) is commonly XFS, so the root-fs requirement is EXT4_FS-or-XFS_FS, not EXT4_FS alone.
    feat = feature_requirement("rootfs_mount")
    symbols = {s for clause in feat.advertised for s in clause}
    assert symbols == {"EXT4_FS", "XFS_FS", "VIRTIO_BLK"}
    for stale in ("SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP"):
        assert stale not in symbols
    assert "squashfs" not in feat.summary.lower()
    assert "overlay" not in feat.summary.lower()


def test_rootfs_mount_root_filesystem_is_an_or_group_not_two_and_clauses():
    # AND-of-OR: two _plain clauses would make every ext4-only local-libvirt kernel warn for a
    # missing XFS_FS (and vice versa). One OR-group keeps the advisory at "mounts nothing kdive
    # boots", which is the only claim kdive can make without a guest-family axis.
    feat = feature_requirement("rootfs_mount")
    assert frozenset({"EXT4_FS", "XFS_FS"}) in feat.advertised
    assert frozenset({"EXT4_FS"}) not in feat.advertised
    assert frozenset({"XFS_FS"}) not in feat.advertised


def test_rhel_guest_kdump_feature_carries_the_symbols_lost_with_the_build_fragment():
    # #1626: ADR-0213 put SQUASHFS/SQUASHFS_ZSTD/BLK_DEV_LOOP/OVERLAY_FS/KEXEC_FILE and ADR-0183
    # put XFS_FS into the ADR-0096 kdump build-config fragment. ADR-0316 deleted the fragment and
    # every symbol but KEXEC_FILE went with it, unnoticed, until #1610's Rocky 10 live run needed
    # five rebuilds to rediscover them. They now live here.
    feat = feature_requirement(CRASH_CAPTURE_RHEL_GUEST)
    symbols = {s for clause in feat.advertised for s in clause}
    assert symbols == {
        "XFS_FS",
        "SQUASHFS",
        "SQUASHFS_ZSTD",
        "EROFS_FS",
        "OVERLAY_FS",
        "BLK_DEV_LOOP",
        "KEXEC_FILE",
    }


def test_rhel_guest_kdump_summary_says_it_is_conditional_and_names_the_dependencies():
    # The issue asks that the set be described as filesystem- and initramfs-dependent rather than
    # implied universal, so a non-RHEL guest knows to skip it.
    summary = feature_requirement(CRASH_CAPTURE_RHEL_GUEST).summary.lower()
    assert "rhel" in summary
    assert "initramfs" in summary
    assert "dracut" in summary
    assert "kexec_file_load" in summary
    assert "non-rhel" in summary


def test_rhel_guest_kdump_names_every_missing_symbol_for_a_bare_defconfig_capture_kernel():
    # The bite: the kernel the #1610 run first uploaded — crash_capture-complete and gate-passing,
    # but with none of the RHEL-family extras — must now come back naming all seven at once,
    # instead of surfacing one per rebuild.
    cfg = KernelConfig(
        frozenset(
            {
                "KEXEC",
                "KEXEC_CORE",
                "CRASH_DUMP",
                "PROC_VMCORE",
                "VMCORE_INFO",
                "FW_CFG_SYSFS",
                "RELOCATABLE",
                "EXT4_FS",
                "VIRTIO_BLK",
            }
        )
    )
    unmet = unmet_advertised_clauses(cfg, feature_requirement(CRASH_CAPTURE_RHEL_GUEST))
    assert missing_symbols(unmet) == [
        "BLK_DEV_LOOP",
        "EROFS_FS",
        "KEXEC_FILE",
        "OVERLAY_FS",
        "SQUASHFS",
        "SQUASHFS_ZSTD",
        "XFS_FS",
    ]


def test_rhel_guest_kdump_is_silent_for_a_kernel_that_carries_the_whole_set():
    cfg = KernelConfig(
        frozenset(
            {
                "XFS_FS",
                "SQUASHFS",
                "SQUASHFS_ZSTD",
                "EROFS_FS",
                "OVERLAY_FS",
                "BLK_DEV_LOOP",
                "KEXEC_FILE",
            }
        )
    )
    assert unmet_advertised_clauses(cfg, feature_requirement(CRASH_CAPTURE_RHEL_GUEST)) == ()


def test_crash_capture_summary_disclaims_being_sufficient_on_a_rhel_guest():
    # The #1626 trap: crash_capture is complete, the gate passes, kexec_crash_size is non-zero,
    # and capture still produces nothing. The base feature must point at the conditional one.
    summary = feature_requirement(CRASH_CAPTURE).summary
    assert CRASH_CAPTURE_RHEL_GUEST in summary


def test_virtio_blk_is_filed_under_rootfs_mount_not_serial_console():
    # The root-disk driver requirement was previously misfiled under serial_console.
    rootfs = feature_requirement("rootfs_mount")
    serial = feature_requirement("serial_console")
    rootfs_symbols = {s for clause in rootfs.advertised for s in clause}
    serial_symbols = {s for clause in serial.advertised for s in clause}
    assert "VIRTIO_BLK" in rootfs_symbols
    assert "VIRTIO_BLK" not in serial_symbols


# #1848: the sanitizer / lock-debugging / tracing / fuzzing / fault-injection / coverage
# features. Every symbol below is read from the kernel's own Kconfig at v7.0 (lib/Kconfig.kasan,
# lib/Kconfig.kcsan, lib/Kconfig.kfence, lib/Kconfig.debug, mm/Kconfig.debug, kernel/trace/Kconfig,
# kernel/bpf/Kconfig, arch/Kconfig), not from memory.
_ADVISORY_DEBUG_FEATURES = (
    "kcsan",
    "kfence",
    "kmemleak",
    "lockdep",
    "ftrace",
    "bpf_tracing",
    "fault_injection",
    "kcov",
)


def test_advisory_debug_features_reach_the_manifest_ungated():
    manifest = {m["feature"]: m for m in feature_manifest()}
    for fid in _ADVISORY_DEBUG_FEATURES:
        assert fid in manifest, fid
        entry = manifest[fid]
        assert entry["gated"] is False, fid
        assert entry["requirements"], fid
        assert entry["summary"], fid


def test_advisory_debug_feature_symbol_sets_match_the_kernel_kconfig():
    expected = {
        # lib/Kconfig.kcsan: "depends on DEBUG_KERNEL && !KASAN"
        "kcsan": {"DEBUG_KERNEL", "KCSAN"},
        # lib/Kconfig.kfence: no DEBUG_KERNEL dependency; the knobs are ints, not booleans
        "kfence": {"KFENCE"},
        # mm/Kconfig.debug: "depends on DEBUG_KERNEL && HAVE_DEBUG_KMEMLEAK"; DEBUG_FS is
        # select-ed, so advertising it could never warn
        "kmemleak": {"DEBUG_KERNEL", "DEBUG_KMEMLEAK"},
        # lib/Kconfig.debug: PROVE_LOCKING selects LOCKDEP (so LOCKDEP is not advertised);
        # DEBUG_ATOMIC_SLEEP is a separate user-selectable symbol PROVE_LOCKING does not select
        "lockdep": {"DEBUG_KERNEL", "PROVE_LOCKING", "DEBUG_ATOMIC_SLEEP"},
        # kernel/trace/Kconfig: TRACING is what builds fs/tracefs; arch/Kconfig: KPROBES
        "ftrace": {
            "FTRACE",
            "TRACING",
            "FUNCTION_TRACER",
            "DYNAMIC_FTRACE",
            "KPROBES",
            "KPROBE_EVENTS",
            "UPROBE_EVENTS",
        },
        # kernel/trace/Kconfig: BPF_EVENTS "depends on BPF_SYSCALL" and
        # "(KPROBE_EVENTS || UPROBE_EVENTS) && PERF_EVENTS"; bare BPF is select-ed by BPF_SYSCALL
        "bpf_tracing": {
            "BPF_SYSCALL",
            "BPF_JIT",
            "PERF_EVENTS",
            "KPROBE_EVENTS",
            "UPROBE_EVENTS",
            "BPF_EVENTS",
            "DEBUG_INFO_BTF",
        },
        # lib/Kconfig.debug: FAULT_INJECTION "depends on DEBUG_KERNEL" and injects nothing alone
        "fault_injection": {
            "DEBUG_KERNEL",
            "FAULT_INJECTION",
            "FAILSLAB",
            "FAIL_PAGE_ALLOC",
            "FAIL_MAKE_REQUEST",
            "FAIL_IO_TIMEOUT",
            "FAIL_FUTEX",
            "FAULT_INJECTION_DEBUG_FS",
            "FAULT_INJECTION_CONFIGFS",
        },
        # lib/Kconfig.debug: KCOV select-s DEBUG_FS itself, so DEBUG_FS is not advertised
        "kcov": {"KCOV", "KCOV_INSTRUMENT_ALL"},
    }
    for fid, symbols in expected.items():
        feat = feature_requirement(fid)
        assert {s for clause in feat.advertised for s in clause} == symbols, fid


def test_interchangeable_kernel_variants_are_or_groups_not_separate_and_clauses():
    # An AND clause per variant would warn on a kernel that legitimately picked the other one.
    # Each pair below is a genuine either/or in the kernel's Kconfig.
    assert frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"}) in feature_requirement("ftrace").advertised
    assert (
        frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"})
        in feature_requirement("bpf_tracing").advertised
    )
    fault = feature_requirement("fault_injection")
    # the two userspace drive interfaces: debugfs or configfs, never both required
    assert frozenset({"FAULT_INJECTION_DEBUG_FS", "FAULT_INJECTION_CONFIGFS"}) in fault.advertised
    # at least one injection site, not all five
    assert (
        frozenset(
            {
                "FAILSLAB",
                "FAIL_PAGE_ALLOC",
                "FAIL_MAKE_REQUEST",
                "FAIL_IO_TIMEOUT",
                "FAIL_FUTEX",
            }
        )
        in fault.advertised
    )


def test_a_configfs_driven_fault_injection_kernel_is_not_told_it_needs_debugfs():
    # The bite for the OR-groups above: a kernel that picked configfs and one injection site is
    # complete, and separate AND clauses would have reported four false missing symbols.
    cfg = KernelConfig(
        frozenset(
            {
                "DEBUG_KERNEL",
                "FAULT_INJECTION",
                "FAILSLAB",
                "FAULT_INJECTION_CONFIGFS",
            }
        )
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("fault_injection")) == ()


def test_advisory_debug_feature_summaries_name_the_bug_class_and_the_runtime_cost():
    # Same bar as test_debuginfo_summary_names_use_case_and_cost: an agent choosing a config
    # needs to know what the feature finds and what it costs, or it enables everything.
    expected = {
        "kcsan": (("data race",), ("slow", "microsecond")),
        "kfence": (("use-after-free", "out-of-bounds"), ("sample", "guard page")),
        "kmemleak": (("leak",), ("scan", "stack trace")),
        "lockdep": (("deadlock", "lock-ordering"), ("every lock", "bookkeeping")),
        "ftrace": (("which code path", "tracepoint"), ("nop", "cost")),
        "bpf_tracing": (("kprobe", "tracepoint"), ("pahole", "attached")),
        "fault_injection": (("error path", "returns null"), ("probability", "cost")),
        "kcov": (("coverage", "fuzz"), ("expensive", "slow")),
    }
    for fid, (bug_class_terms, cost_terms) in expected.items():
        summary = feature_requirement(fid).summary.lower()
        assert any(term in summary for term in bug_class_terms), f"{fid}: no bug class"
        assert any(term in summary for term in cost_terms), f"{fid}: no runtime cost"
        # every summary must state whether it composes with debuginfo or not
        assert "debuginfo" in summary, f"{fid}: no debuginfo composition note"


def test_advisory_debug_feature_summaries_carry_no_adr_citation():
    # These strings ship inside a registered MCP resource; tests/mcp/core/test_no_adr_leak.py
    # walks the whole served surface, this keeps the failure local to the registry.
    import re

    for feat in FEATURE_REQUIREMENTS:
        assert not re.search(r"ADR-\d+", feat.summary), feat.feature


def test_kcsan_and_kasan_are_advertised_as_mutually_exclusive():
    # lib/Kconfig.kcsan: "depends on DEBUG_KERNEL && !KASAN". An agent that enables both gets a
    # kernel with no KCSAN at all and no error, so both summaries have to say so.
    assert "kasan" in feature_requirement("kcsan").summary.lower()
    assert "kcsan" in feature_requirement("kasan").summary.lower()


def test_kasan_summary_names_the_bug_class_the_memory_cost_and_the_debuginfo_tradeoff():
    # #1848: the entry used to read "Kernel Address Sanitizer instrumentation." - five words that
    # named neither what it finds nor that it costs an eighth of RAM, against the bar the
    # debuginfo entry sets. #1350 is the same failure mode one entry over.
    summary = feature_requirement("kasan").summary.lower()
    assert "use-after-free" in summary
    assert "out-of-bounds" in summary
    assert "1/8" in summary  # shadow memory, lib/Kconfig.kasan help text
    assert "3x" in summary  # documented slowdown
    assert "omit" in summary  # explicit guidance for the case that does not need it
    assert "debuginfo" in summary


def test_kasan_mode_and_instrumentation_choices_are_or_groups():
    # lib/Kconfig.kasan has two `choice` blocks: mode (GENERIC / SW_TAGS / HW_TAGS) and
    # instrumentation (INLINE / OUTLINE). Choice members are mutually exclusive, so the old
    # _plain("KASAN", "KASAN_INLINE") reported a missing KASAN_INLINE on every outline kernel.
    feat = feature_requirement("kasan")
    assert frozenset({"KASAN_GENERIC", "KASAN_SW_TAGS", "KASAN_HW_TAGS"}) in feat.advertised
    assert frozenset({"KASAN_INLINE", "KASAN_OUTLINE"}) in feat.advertised
    assert frozenset({"KASAN_INLINE"}) not in feat.advertised


def test_an_outline_instrumented_kasan_kernel_is_advertised_as_complete():
    # The bite: this kernel is a working KASAN kernel and used to come back missing KASAN_INLINE.
    cfg = KernelConfig(
        frozenset({"KASAN", "KASAN_GENERIC", "KASAN_OUTLINE", "STACKTRACE"}),
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("kasan")) == ()


def test_a_kernel_with_no_sanitizer_at_all_is_told_what_kasan_needs():
    # Non-vacuity guard for the test above: the same check must still report on a bare kernel.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("kasan")))
    assert "KASAN" in missing
    assert "KASAN_GENERIC" in missing
    assert "KASAN_OUTLINE" in missing
