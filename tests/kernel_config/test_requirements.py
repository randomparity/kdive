from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    CRASH_CAPTURE_RHEL_GUEST,
    FEATURE_REQUIREMENTS,
    ROOTFS_MOUNT,
    SYSRQ,
    Clause,
    FeatureRequirement,
    feature_manifest,
    feature_requirement,
)
from kdive.kernel_config.support import (
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)
from tests.kernel_config.unsettable_symbols import I1_SEED, UNSETTABLE_SYMBOLS


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause.symbols}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause.symbols}
    assert Clause(frozenset({"KEXEC", "KEXEC_FILE"})) in feat.gate_required  # either load syscall
    assert feat.gated is True


def test_only_the_named_gate_consumers_are_gated_and_the_rest_advertise_only():
    # #1848: this used to iterate six literal ids, so an advertise-only feature added later was
    # silently uncovered. Deriving both sets from the roster fixes that in both directions - a
    # new advertise-only feature is checked without editing the test, and a new *gated* feature
    # fails here on purpose. #1861 narrowed the set to crash_capture alone: gate.py imports
    # CRASH_CAPTURE and ROOTFS_MOUNT, and only crash-capture arming turns a refusal set into a
    # refusal (rootfs_mount is advertise-only and warns off the advertised clauses), so growing
    # the gated set is a decision, not a data addition.
    gated = {f.feature for f in FEATURE_REQUIREMENTS if f.gated}
    assert gated == {CRASH_CAPTURE}

    advertise_only = [f for f in FEATURE_REQUIREMENTS if f.feature not in gated]
    for feat in advertise_only:
        assert feat.gate_required == (), feat.feature
        assert feat.gated is False, feat.feature
        # an advertise-only feature that advertises nothing would pass the two asserts above
        # while telling the agent nothing at all
        assert feat.advertised, feat.feature
        assert feat.summary.strip(), feat.feature


def test_sysrq_advertises_magic_sysrq_and_carries_no_refusal_set():
    # #1861: the entry used to carry gate_required=MAGIC_SYSRQ that no seam read, and `gated` is
    # derived from it, so feature_manifest() shipped `"gated": true` to an agent while the upload
    # path checked nothing. ADR-0318 decided sysrq is advertised and enforced by the runtime
    # detection in diagnostic_sysrq, so the refusal set is empty and the manifest says so.
    feat = feature_requirement(SYSRQ)
    assert feat.advertised == (Clause(frozenset({"MAGIC_SYSRQ"})),)
    assert feat.gate_required == ()
    assert feat.gated is False
    entry = next(m for m in feature_manifest() if m["feature"] == SYSRQ)
    assert entry["gated"] is False
    # non-vacuity: the entry a reader gets must really advertise the symbol, otherwise a
    # gated: false entry with an empty requirements list would pass the assert above while
    # telling an agent nothing about what to build
    assert entry["requirements"] == [{"symbols": ["MAGIC_SYSRQ"]}]


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


def test_ikconfig_summary_names_the_readback_use_case_the_skip_case_and_that_it_is_cheap():
    # #1851: the entry read "Read the running kernel's own config back via /proc/config.gz." -
    # the mechanism and nothing else. An agent had no basis to include or omit it, so the
    # summary must name why you would want the readback (olddefconfig silently drops a symbol
    # whose dependencies are unmet, so what you set and what you got can differ), when it is
    # redundant (you kept the .config and uploaded it as effective_config), and that the price
    # is a gzipped blob in .rodata rather than anything at runtime. init/Kconfig:767 IKCONFIG is
    # a tristate, so the built-in-versus-module note is a real choice the agent has to make.
    summary = feature_requirement("ikconfig").summary.lower()
    # what it enables
    assert "/proc/config.gz" in summary
    assert "olddefconfig" in summary
    # when to skip
    assert "effective_config" in summary
    assert "skip" in summary
    # what it costs, and that the cost is close to nothing
    assert "no runtime cost" in summary
    assert "kilobytes" in summary
    # tristate: a module gives you the file only while it is loaded
    assert "module" in summary


def test_sysrq_summary_names_the_use_case_the_late_refusal_and_that_it_is_build_time_only():
    # #1851: the entry read "Inject magic SysRq diagnostics from the host." - it never said that
    # omitting MAGIC_SYSRQ is unrecoverable, so an agent met the refusal at the diagnostic with
    # no warning. lib/Kconfig.debug:665 makes MAGIC_SYSRQ a plain bool (no module form, nothing
    # to turn on afterwards), which is why the summary has to say so before the agent commits.
    #
    # Where the refusal lands is the load-bearing half and the easiest thing to get wrong.
    # sysrq is not pre-gated: gate.py loads CRASH_CAPTURE and ROOTFS_MOUNT only, and ADR-0318
    # chose runtime detection on purpose so kdive never refuses a working sysrq off a stale
    # Run's config. A summary that claimed a config-time gate would tell an agent a clean upload
    # had cleared it, which is the wasted rebuild #1851 exists to stop - so the upload-time
    # disclaimer is asserted, not just the seam name.
    summary = feature_requirement(SYSRQ).summary.lower()
    # what it enables: the diagnostics kdive actually exposes, on a guest that stopped answering
    assert "wedged" in summary or "no longer answer" in summary
    assert "task states" in summary
    # the refusal is late, and the summary must say so rather than imply an upload-time check
    assert "nothing checks this at upload" in summary
    assert "magic_sysrq" in summary
    assert "diagnostic_sysrq" in summary
    assert "configuration error" in summary
    # unrecoverable in place: a bool, so recovering means another build/install/boot round
    assert "rebuild" in summary
    # the second, runtime half an agent otherwise rediscovers the hard way
    assert "kernel.sysrq" in summary
    # what it costs, and that the cost is close to nothing
    assert "kernel text" in summary
    assert "nothing at runtime" in summary
    # when to skip
    assert "skip" in summary


def test_no_upload_seam_reads_the_sysrq_refusal_set():
    # The reality the summary's "nothing checks this at upload time" rests on, anchored to code
    # rather than to wording - a blocklist of bad phrasings only catches wordings someone
    # already thought of. gate.py is the only module that turns a FeatureRequirement into a
    # refusal, and it names the features it refuses on by importing their ids. If sysrq is ever
    # wired in there, this fails and forces the summary to be rewritten in the same change.
    import inspect

    from kdive.kernel_config import gate

    # the whole module text, not just its import surface: gating sysrq by a string literal or a
    # module-qualified requirements.SYSRQ would leave an attribute check green while making the
    # summary's disclaimer false
    source = inspect.getsource(gate).lower()
    assert "sysrq" not in source
    # non-vacuity, both halves: the module this reads must really be the refusal seam, and the
    # search must really be able to find a feature id in it. The attribute pair also covers the
    # one shape the text search cannot see - a rewrite that drops the named consumers for a loop
    # over FEATURE_REQUIREMENTS would gate sysrq without ever spelling it.
    assert hasattr(gate, "CRASH_CAPTURE")
    assert hasattr(gate, "ROOTFS_MOUNT")
    assert CRASH_CAPTURE in source
    assert ROOTFS_MOUNT in source


def test_sysrq_summary_does_not_promise_a_config_time_gate_the_upload_path_never_performs():
    # The inverse of the disclaimer assertion above, because the two fail on different edits:
    # dropping the disclaimer trips that test, while *adding* a "kdive gates this" claim beside
    # it would leave it green.
    summary = feature_requirement(SYSRQ).summary.lower()
    for claim in (
        "kdive gates this",
        "gated at upload",
        "refuses the upload",
        "the gate refuses",
        "kdive checks this at upload",
        "the config gate covers this feature",
        "without magic_sysrq",
    ):
        assert claim not in summary, claim
    # Non-vacuity: every assert above is a negative, so an empty or truncated summary would pass
    # the whole loop. Anchor it to text the entry really carries, and to the substring search
    # really being able to find that text.
    assert "magic_sysrq" in summary
    assert "nothing checks this at upload time" in summary
    # #1851 had to spend a sentence explaining that the entry's gated: true meant the late
    # refusal rather than an upload check. #1861 removed the refusal set instead, so the flag
    # now agrees with the upload path and the summary must not reintroduce the explanation.
    assert "gated flag" not in summary
    assert feature_requirement(SYSRQ).gated is False


def test_serial_console_summary_names_what_breaks_without_it_and_that_it_is_cheap():
    # #1851: the entry read "Serial console + virtio devices the local-libvirt profile expects."
    # - it named a profile, not a consequence. The console is the only channel kdive has for
    # kernel output from a guest with no working SSH, and VIRTIO_PCI is the transport the
    # rootfs_mount virtio-blk disk binds through, so omitting these does not degrade an
    # investigation, it ends one before boot. drivers/tty/serial/8250/Kconfig:72 "depends on
    # SERIAL_8250=y" makes the built-in requirement real, and drivers/tty/hvc/Kconfig:14
    # HVC_CONSOLE is the ppc64le answer - SERIAL_8250_CONSOLE does nothing on a pseries guest,
    # whose console is hvc0, so the summary may not present the 8250 symbol as universal.
    raw = feature_requirement("serial_console").summary
    summary = raw.lower()
    # what it enables
    assert "panic" in summary
    assert "ttys0" in summary
    # the arch split: the advertised 8250 symbol is the x86 answer only
    assert "hvc0" in summary
    assert "hvc_console" in summary
    # the boot-fatal half: VIRTIO_PCI is how the virtio-blk root disk is reached
    assert "virtio_pci" in summary
    assert "rootfs_mount" in summary
    assert "boot-fatal" in summary
    # kdive checks neither of these symbols at any value, so the summary must not imply that
    # omitting one would be caught - it is the only notice an agent gets
    assert "nothing below is checked" in summary
    # when to skip: never, on a guest kdive boots - and the summary must say so outright
    assert "no reason to skip" in summary
    # what it costs, and that the cost is close to nothing
    assert "kernel text" in summary
    # both symbols are modular-capable in the wrong way: SERIAL_8250_CONSOLE is not offered
    # against a modular 8250, and a modular VIRTIO_PCI cannot be loaded before root is mounted
    assert "rather than as a module" in summary
    assert "=y" in raw
    # #1863: the "no initramfs" premise under that build-it-in advice is conditional, and this
    # carve-out has regressed once already - 6d0a61891 deleted it while scoping the claim to the
    # local-libvirt boot, and 2d03f3c51 had to restore it. It was unpinned through both, so pin
    # it here rather than only on the rootfs_mount copy that has never regressed.
    assert "unless your build uploads an initrd artifact" in summary


def test_unknown_feature_raises():
    import pytest

    with pytest.raises(KeyError):
        feature_requirement("does_not_exist")


def test_rootfs_mount_matches_the_real_direct_kernel_boot():
    # #1094: rootfs_mount used to advertise a squashfs+overlay boot path that does not exist
    # anywhere in the tree. Those stay out — the boot kdive stages at install (ADR-0030) is a
    # whole-disk qcow2 mounted direct-kernel via root=/dev/vda (a virtio-blk device), with no
    # initramfs unless the Run's build uploaded an initrd artifact (#1863). The provisioning
    # baseline boot is a different boot and not what this entry describes: it stages the base
    # image's own initramfs (ADR-0272, select_kernel_and_initrd).
    # #1626 refines the filesystem half only: a remote or agent-uploaded rootfs (ADR-0183/0440/
    # 0441) is commonly XFS, so the root-fs requirement is EXT4_FS-or-XFS_FS, not EXT4_FS alone.
    feat = feature_requirement("rootfs_mount")
    symbols = {s for clause in feat.advertised for s in clause.symbols}
    assert symbols == {"EXT4_FS", "XFS_FS", "VIRTIO_BLK"}
    for stale in ("SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP"):
        assert stale not in symbols
    assert "squashfs" not in feat.summary.lower()
    assert "overlay" not in feat.summary.lower()


def test_rootfs_mount_summary_qualifies_the_no_initramfs_claim():
    # #1863: the parenthetical read "(root=/dev/vda, no initramfs)" unconditionally, which is
    # false for any Run whose build_result carries an initrd_ref - lifecycle/install.py:430-436
    # stages it and :539-540 emits the <initrd> element onto the already-defined domain. (Not
    # lifecycle/xml.py: that renderer serves the provisioning and customization boots, which is
    # a different boot from the one this entry describes.) #1851 had already qualified the same
    # claim in serial_console, so one feature_config_requirements payload shipped both forms.
    # The unqualified form is the load-bearing half: it is the premise behind "build the driver
    # in, there is nothing to load a module from", so an agent reading it as absolute draws a
    # stronger conclusion than the boot path supports.
    summary = feature_requirement("rootfs_mount").summary.lower()
    # Both the claim and its exception are asserted inside the parenthetical, so they are proven
    # co-located: an agent that stops reading at the claim cannot take it as absolute. Slicing
    # the parenthetical rather than measuring a character distance keeps this indifferent to
    # word order, and unlike splitting on punctuation it cannot silently widen to the whole
    # summary. A summary with no such parenthetical raises here, which fails closed.
    start = summary.index("(root=/dev/vda")
    paren = summary[start : summary.index(")", start) + 1]
    assert "no initramfs" in paren
    assert "unless your build uploads an initrd artifact" in paren


def test_rootfs_mount_root_filesystem_is_an_or_group_not_two_and_clauses():
    # AND-of-OR: two _plain clauses would make every ext4-only local-libvirt kernel warn for a
    # missing XFS_FS (and vice versa). One OR-group keeps the advisory at "mounts nothing kdive
    # boots", which is the only claim kdive can make without a guest-family axis.
    feat = feature_requirement("rootfs_mount")
    assert Clause(frozenset({"EXT4_FS", "XFS_FS"})) in feat.advertised
    assert Clause(frozenset({"EXT4_FS"})) not in feat.advertised
    assert Clause(frozenset({"XFS_FS"})) not in feat.advertised


def test_rhel_guest_kdump_feature_carries_the_symbols_lost_with_the_build_fragment():
    # #1626: ADR-0213 put SQUASHFS/SQUASHFS_ZSTD/BLK_DEV_LOOP/OVERLAY_FS/KEXEC_FILE and ADR-0183
    # put XFS_FS into the ADR-0096 kdump build-config fragment. ADR-0316 deleted the fragment and
    # every symbol but KEXEC_FILE went with it, unnoticed, until #1610's Rocky 10 live run needed
    # five rebuilds to rediscover them. They now live here.
    feat = feature_requirement(CRASH_CAPTURE_RHEL_GUEST)
    symbols = {s for clause in feat.advertised for s in clause.symbols}
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
    rootfs_symbols = {s for clause in rootfs.advertised for s in clause.symbols}
    serial_symbols = {s for clause in serial.advertised for s in clause.symbols}
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


def test_advisory_debug_feature_clause_sets_are_the_reviewed_kconfig_sourced_ones():
    # Asserts the clause *tuple*, not the flattened symbol union: regrouping an OR-group into
    # separate AND clauses (the exact regression #1848 fixes in kasan) leaves the union
    # unchanged and would slip past a set comparison. Symbols and grouping are read from the
    # kernel's own Kconfig at v7.0; the file:line citations are in the comments below.
    expected = {
        # lib/Kconfig.kcsan:16 "depends on DEBUG_KERNEL && !KASAN"
        "kcsan": (Clause(frozenset({"DEBUG_KERNEL"})), Clause(frozenset({"KCSAN"}))),
        # lib/Kconfig.kfence:8 has no DEBUG_KERNEL dependency; the knobs are ints, not booleans,
        # and parse_kernel_config only counts =y/=m as enabled
        "kfence": (Clause(frozenset({"KFENCE"})),),
        # mm/Kconfig.debug:242 "depends on DEBUG_KERNEL && HAVE_DEBUG_KMEMLEAK"; :243 select-s
        # DEBUG_FS, so advertising DEBUG_FS could never warn
        "kmemleak": (Clause(frozenset({"DEBUG_KERNEL"})), Clause(frozenset({"DEBUG_KMEMLEAK"}))),
        # lib/Kconfig.debug:1452-1458 PROVE_LOCKING select-s LOCKDEP and the DEBUG_* lock set,
        # so none of those is advertised; :1650 DEBUG_ATOMIC_SLEEP is separate and prompted
        "lockdep": (
            Clause(frozenset({"DEBUG_KERNEL"})),
            Clause(frozenset({"PROVE_LOCKING"})),
            Clause(frozenset({"DEBUG_ATOMIC_SLEEP"})),
        ),
        # kernel/trace/Kconfig:179 TRACING and :301 DYNAMIC_FTRACE are prompt-less bools the
        # kernel turns on itself, so they are prose, not requirements. arch/Kconfig:117 KPROBES.
        "ftrace": (
            Clause(frozenset({"FTRACE"})),
            Clause(frozenset({"FUNCTION_TRACER"})),
            Clause(frozenset({"KPROBES"})),
            Clause(frozenset({"KPROBE_EVENTS"})),
        ),
        # kernel/trace/Kconfig:853-856 BPF_EVENTS is a prompt-less default-y bool that "depends
        # on BPF_SYSCALL" and "(KPROBE_EVENTS || UPROBE_EVENTS) && PERF_EVENTS" - so that OR is
        # the real either/or, and BPF_EVENTS itself is derived from the other three, not set.
        # kernel/bpf/Kconfig:4 bare BPF is select-ed by BPF_SYSCALL; :42 BPF_JIT is a codegen
        # speedup, not a prerequisite - programs attach and run under the interpreter without it.
        "bpf_tracing": (
            Clause(frozenset({"BPF_SYSCALL"})),
            Clause(frozenset({"PERF_EVENTS"})),
            Clause(frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"})),
            Clause(frozenset({"DEBUG_INFO_BTF"})),
        ),
        # lib/Kconfig.debug:2085 FAULT_INJECTION "depends on DEBUG_KERNEL" and injects nothing
        # alone; :2137 FAULT_INJECTION_DEBUG_FS "depends on FAULT_INJECTION && SYSFS && DEBUG_FS".
        # Only FAIL_FUTEX select-s DEBUG_FS (:2130); the other four sites do not, so the clause
        # is a real requirement for any of them. FAULT_INJECTION_CONFIGFS is
        # NOT an alternative: all five sites register through fault_create_debugfs_attr(), which
        # lib/fault-inject.c:188 compiles only under CONFIG_FAULT_INJECTION_DEBUG_FS.
        "fault_injection": (
            Clause(frozenset({"DEBUG_KERNEL"})),
            Clause(frozenset({"FAULT_INJECTION"})),
            Clause(
                frozenset(
                    {
                        "FAILSLAB",
                        "FAIL_PAGE_ALLOC",
                        "FAIL_MAKE_REQUEST",
                        "FAIL_IO_TIMEOUT",
                        "FAIL_FUTEX",
                    }
                )
            ),
            Clause(frozenset({"FAULT_INJECTION_DEBUG_FS"})),
            Clause(frozenset({"DEBUG_FS"})),
            Clause(frozenset({"SYSFS"})),
        ),
        # lib/Kconfig.debug:2210 KCOV select-s DEBUG_FS itself; :2228 KCOV_INSTRUMENT_ALL is a
        # prompted default-y knob whose help tells targeted fuzzing to turn it off, so requiring
        # it would contradict the summary's own advice
        "kcov": (Clause(frozenset({"KCOV"})),),
    }
    for fid, clauses in expected.items():
        assert feature_requirement(fid).advertised == clauses, fid


def test_a_kernel_that_picked_one_injection_site_is_not_told_it_needs_the_other_four():
    # The bite for the fault_injection OR-group: five separate AND clauses would report four
    # false missing symbols against a complete failslab-only kernel.
    cfg = KernelConfig(
        frozenset(
            {
                "DEBUG_KERNEL",
                "FAULT_INJECTION",
                "FAILSLAB",
                "FAULT_INJECTION_DEBUG_FS",
                "DEBUG_FS",
                "SYSFS",
            }
        )
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("fault_injection")) == ()


def test_a_configfs_only_fault_injection_kernel_is_told_it_still_needs_the_debugfs_interface():
    # FAULT_INJECTION_CONFIGFS looks like an alternative and is not: lib/fault-inject.c:188 gates
    # fault_create_debugfs_attr() - the only registration path FAILSLAB and friends use - on
    # CONFIG_FAULT_INJECTION_DEBUG_FS. Advertising the two as an OR-group would have called this
    # kernel complete while it exposes no knob to set a failure rate with.
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
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("fault_injection")))
    assert missing == ["DEBUG_FS", "FAULT_INJECTION_DEBUG_FS", "SYSFS"]


def test_either_probe_event_source_satisfies_the_bpf_tracing_dependency():
    # kernel/trace/Kconfig:855: BPF_EVENTS needs KPROBE_EVENTS *or* UPROBE_EVENTS, so a
    # uprobe-only kernel is complete and two AND clauses would falsely fault it.
    cfg = KernelConfig(
        frozenset(
            {
                "BPF_SYSCALL",
                "PERF_EVENTS",
                "UPROBE_EVENTS",
                "DEBUG_INFO_BTF",
            }
        )
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("bpf_tracing")) == ()


def test_no_clause_of_any_feature_names_a_symbol_the_agent_cannot_set():
    # Invariant I1 of the clause model #1854 settles. A prompt-less or auto-select-ed symbol
    # cannot be set from a config fragment - olddefconfig discards it - so reporting one as
    # missing sends the agent after the one thing it cannot do. #1850 held this over `advertised`
    # only, which let crash_capture keep KEXEC_CORE and VMCORE_INFO in `gate_required`, where the
    # same symbols reach the agent through a *refusal* - the louder channel. The rule bars them
    # from every clause, so this walks both fields.
    #
    # THIS IS A REGRESSION GUARD, NOT A PROOF. It catches the return of a symbol already known to
    # be unsettable and cannot catch a tenth: nothing in a .config distinguishes a prompt-less
    # symbol from a prompted one, so the only real check is a human reading Kconfig, and the list
    # grows as symbols are verified. It is not a claim that every clause has been audited.
    #
    # It also says nothing about a symbol that is settable *behind a prerequisite*:
    # SERIAL_8250_CONSOLE and DEBUG_INFO_BTF are clause members by design, with the prerequisite
    # carried as its own clause, and are not candidates for the list.
    #
    # The seed must stay listed: without this, deleting a row as "not named anywhere anyway"
    # would defang the guard against re-adding it, silently.
    assert set(UNSETTABLE_SYMBOLS) >= I1_SEED, sorted(I1_SEED - set(UNSETTABLE_SYMBOLS))
    named = _every_clause_symbol(FEATURE_REQUIREMENTS)
    assert named  # non-vacuity: the walk must actually see the roster
    # The failure message names the Kconfig file:line of every offender so a new entry can be
    # checked against the kernel directly.
    leaked = named & set(UNSETTABLE_SYMBOLS)
    assert not leaked, {symbol: UNSETTABLE_SYMBOLS[symbol] for symbol in sorted(leaked)}


def _every_clause_symbol(features: tuple[FeatureRequirement, ...]) -> set[str]:
    return {
        symbol
        for f in features
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        for symbol in clause.symbols
    }


def test_the_i1_walk_reaches_the_refusal_set_and_not_only_the_advertised_one():
    # The half #1850's version could not have caught, and the half nothing else here proves.
    # Every symbol crash_capture gates is also advertised, so a walk that silently dropped
    # gate_required would leave the invariant above green against the live roster while the
    # refusal set is exactly where #1854's defect lived. Feed the walk a feature that names an
    # unsettable symbol in gate_required ONLY, and require it to be seen.
    smuggled = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the walk above",
        advertised=(Clause(frozenset({"KEXEC"})),),
        gate_required=(Clause(frozenset({"KEXEC_CORE"})),),
    )
    named = _every_clause_symbol((smuggled,))
    assert named & set(UNSETTABLE_SYMBOLS) == {"KEXEC_CORE"}
    # and the advertised half is still walked, so neither field can be dropped unnoticed
    assert "KEXEC" in named


def test_debuginfo_advertises_the_settable_debug_info_producers_not_the_symbol_they_imply():
    # #1850. lib/Kconfig.debug:249 DEBUG_INFO is a bare prompt-less bool. Both routes to the clause
    # below imply it: DEBUG_INFO_DWARF4 (:293) and DEBUG_INFO_DWARF5 (:305) select it (:295, :307),
    # and DEBUG_INFO_BTF (:398) is not a choice member at all - it sits inside `if DEBUG_INFO`
    # (:325-455), so it cannot be y while DEBUG_INFO is n. DEBUG_INFO=n therefore forces every
    # member of the clause off, and that clause reports the same kernel without naming a symbol
    # the agent cannot set.
    feat = feature_requirement("debuginfo")
    assert feat.advertised == (
        Clause(frozenset({"DEBUG_INFO_DWARF5", "DEBUG_INFO_DWARF4", "DEBUG_INFO_BTF"})),
        Clause(frozenset({"DEBUG_KERNEL"})),
    )


def test_a_kernel_with_no_debug_info_is_still_told_which_settable_symbols_to_build_in():
    # Non-vacuity guard for the removal above: dropping the DEBUG_INFO clause must not make the
    # advisory quieter on the kernel it exists to catch, only shorter by the unsettable symbol.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("debuginfo")))
    assert missing == ["DEBUG_INFO_BTF", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF5", "DEBUG_KERNEL"]


def test_crash_capture_advertises_the_kexec_prompts_not_the_symbols_they_select():
    # #1850. kernel/Kconfig.kexec:11 KEXEC_CORE and :8 VMCORE_INFO are bare prompt-less bools.
    # KEXEC (:20) and KEXEC_FILE (:38) select KEXEC_CORE at :23 and :42; CRASH_DUMP (:97) selects
    # VMCORE_INFO at :102, as does PROC_KCORE (fs/proc/Kconfig:32) at :35. Every selector this
    # entry could name is already advertised, so each derived symbol is off only when a selector
    # the same entry reports is off - the two clauses added unsettable names and no signal.
    feat = feature_requirement(CRASH_CAPTURE)
    advertised = {s for clause in feat.advertised for s in clause.symbols}
    assert "KEXEC_CORE" not in advertised
    assert "VMCORE_INFO" not in advertised
    for selector in ("KEXEC", "KEXEC_FILE", "CRASH_DUMP"):
        assert selector in advertised, selector


def test_the_crash_capture_refusal_set_no_longer_names_the_derived_symbols():
    # #1854, replacing #1850's
    # test_unadvertising_the_derived_symbols_leaves_the_crash_capture_refusal_set_untouched.
    # That test pinned the derived pair *into* gate_required on the reasoning that a derived
    # symbol is still provably absent in a parsed .config. True, but the refusal it produced put
    # KEXEC_CORE and VMCORE_INFO into `missing` beside a remediation reading "rebuild the kernel
    # with the missing CONFIG_*" - advice olddefconfig discards, on the channel that blocks the
    # arm rather than merely warning.
    #
    # The pair carries no signal that the surviving clauses do not: KEXEC/KEXEC_FILE select
    # KEXEC_CORE and CRASH_DUMP selects VMCORE_INFO, and each of those selectors is itself a
    # clause here, so no config olddefconfig can produce lacks a derived symbol while its
    # selector is present. Removing them narrows the refusal only on an internally inconsistent
    # upload, which is the false refusal ADR-0318's fail-open boundary exists to avoid.
    feat = feature_requirement(CRASH_CAPTURE)
    derived = {"KEXEC_CORE", "VMCORE_INFO"}
    gate_symbols = {s for clause in feat.gate_required for s in clause.symbols}
    assert derived.isdisjoint(gate_symbols)
    assert derived.isdisjoint({s for clause in feat.advertised for s in clause.symbols})
    # non-vacuity: the refusal set must still be the crash set, not emptied on the way past
    assert gate_symbols == {
        "KEXEC",
        "KEXEC_FILE",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }


def test_a_kernel_that_lacks_only_the_derived_symbols_is_armed_rather_than_refused():
    # The payload assertion #1854 asks for, stated as the behaviour change: the config below is
    # what `make olddefconfig` produces for an agent that set every selector this entry names.
    # It carries neither KEXEC_CORE nor VMCORE_INFO because the kernel writes those itself, and
    # before #1854 it drew a refusal listing exactly the two symbols the agent could not add.
    cfg = KernelConfig(
        frozenset(
            {"KEXEC", "KEXEC_FILE", "CRASH_DUMP", "PROC_VMCORE", "FW_CFG_SYSFS", "RELOCATABLE"}
        )
    )
    assert unmet_clauses(cfg, feature_requirement(CRASH_CAPTURE)) == ()


def test_a_kernel_that_genuinely_cannot_kexec_is_still_refused_and_named():
    # The other direction, so the removal above cannot be read as "the gate got quieter". Drop a
    # settable symbol and the refusal is unchanged - and every symbol it names is one a config
    # fragment can set, which is what makes the gate's "rebuild with the missing CONFIG_*"
    # remediation followable.
    cfg = KernelConfig(frozenset({"KEXEC", "KEXEC_FILE", "CRASH_DUMP", "RELOCATABLE"}))
    missing = missing_symbols(unmet_clauses(cfg, feature_requirement(CRASH_CAPTURE)))
    assert missing == ["FW_CFG_SYSFS", "PROC_VMCORE"]
    assert set(missing).isdisjoint(UNSETTABLE_SYMBOLS)


def test_a_kernel_with_no_kexec_at_all_is_still_told_which_settable_symbols_to_build_in():
    # Non-vacuity guard for the crash_capture removal: the advisory on a bare kernel must still
    # name every symbol the agent can act on, minus the two it cannot.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement(CRASH_CAPTURE)))
    assert missing == [
        "CRASH_DUMP",
        "FW_CFG_SYSFS",
        "KEXEC",
        "KEXEC_FILE",
        "PROC_VMCORE",
        "RANDOMIZE_BASE",
        "RELOCATABLE",
    ]


def test_advisory_debug_feature_summaries_name_the_bug_class_and_the_runtime_cost():
    # Same bar as test_debuginfo_summary_names_use_case_and_cost: an agent choosing a config
    # needs to know what the feature finds and what it costs, or it enables everything.
    expected = {
        "kcsan": (("data race",), ("slow", "microsecond")),
        "kfence": (("use-after-free", "out-of-bounds"), ("sample", "guard page")),
        "kmemleak": (("leak",), ("scan", "stack trace")),
        "lockdep": (("deadlock", "lock-ordering"), ("every lock", "bookkeeping")),
        "ftrace": (("which code path", "tracepoint"), ("nop",)),
        "bpf_tracing": (("kprobe", "tracepoint"), ("pahole", "attached")),
        "fault_injection": (("error path", "returns null"), ("probability",)),
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


def test_kasan_advertises_the_mode_choice_as_an_or_group_and_leaves_instrumentation_to_prose():
    # lib/Kconfig.kasan:73 is a `choice` over GENERIC / SW_TAGS / HW_TAGS - mutually exclusive,
    # so one OR-group, not three AND clauses. The instrumentation `choice` at :141 is
    # "depends on KASAN_GENERIC || KASAN_SW_TAGS", so neither INLINE nor OUTLINE is settable
    # under hardware tag-based mode; advertising that pair as a clause would fault a working
    # HW_TAGS kernel for a symbol it cannot set. The summary carries the choice instead.
    feat = feature_requirement("kasan")
    assert Clause(frozenset({"KASAN_GENERIC", "KASAN_SW_TAGS", "KASAN_HW_TAGS"})) in feat.advertised
    advertised = {s for clause in feat.advertised for s in clause.symbols}
    assert "KASAN_INLINE" not in advertised
    assert "KASAN_OUTLINE" not in advertised
    summary = feat.summary
    assert "KASAN_INLINE" in summary
    assert "KASAN_OUTLINE" in summary


def test_every_kasan_mode_is_advertised_as_complete_including_hardware_tag_based():
    # The bite: the old _plain("KASAN", "KASAN_INLINE") told an outline kernel it was missing
    # KASAN_INLINE, and an INLINE/OUTLINE OR-group would tell an arm64 MTE kernel the same.
    for mode in ("KASAN_GENERIC", "KASAN_SW_TAGS", "KASAN_HW_TAGS"):
        cfg = KernelConfig(frozenset({"KASAN", mode, "STACKTRACE"}))
        assert unmet_advertised_clauses(cfg, feature_requirement("kasan")) == (), mode


def test_a_kernel_with_no_sanitizer_at_all_is_told_what_kasan_needs():
    # Non-vacuity guard for the test above: the same check must still report on a bare kernel.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("kasan")))
    assert missing == ["KASAN", "KASAN_GENERIC", "KASAN_HW_TAGS", "KASAN_SW_TAGS", "STACKTRACE"]
