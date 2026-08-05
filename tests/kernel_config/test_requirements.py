from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final, NamedTuple, cast

import pytest

# The one import of the provisioning platform in this package's tests, and it is deliberately
# here and not in `src/`: ADR-0544 §3 checks every `Clause.arches` value against the arches kdive
# can actually provision, from the test tree only, so `kernel_config` keeps taking no runtime
# dependency on `domain.platform` (pinned by tests/kernel_config/test_layering.py).
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES, arch_traits
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    CRASH_CAPTURE_RHEL_GUEST,
    ENFORCEMENT_LEGEND,
    FEATURE_REQUIREMENTS,
    ROOTFS_MOUNT,
    SYSRQ,
    BuiltIn,
    Clause,
    Enforcement,
    FeatureRequirement,
    enforcement_legend,
    feature_manifest,
    feature_requirement,
)
from kdive.kernel_config.support import (
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)
from kdive.serialization import JsonValue
from tests.kernel_config.config_fixtures import all_builtin
from tests.kernel_config.unsettable_symbols import UNSETTABLE_SYMBOLS

# lib/Kconfig.debug:262-323 is the "Debug information" `choice`; these are its three non-`NONE`
# members, each of which selects DEBUG_INFO and yields real DWARF. Spelled out here rather than
# imported from the registry, so this file stays an independent pin on what the registry says -
# importing the constant the code builds its clauses from would make those assertions circular.
_DWARF_CHOICE = frozenset(
    {"DEBUG_INFO_DWARF5", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT"}
)

# A throwaway non-empty clause tuple, for constructing FeatureRequirements that exist only to
# exercise the construction-time guard. Non-empty because an entry advertising nothing is its own
# defect and is asserted against elsewhere.
_CLAUSE: Final[tuple[Clause, ...]] = (Clause(frozenset({"A_SYMBOL"})),)


def _clause_symbols(clauses: JsonValue) -> set[str]:
    """Flatten a rendered clause array's symbols. The cast is the JsonValue union, not a claim -
    the element shape it assumes is itself asserted structurally in the served-document tests."""
    return {
        symbol
        for clause in cast(list[dict[str, list[str]]], clauses)
        for symbol in clause["symbols"]
    }


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause.symbols}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause.symbols}
    assert Clause(frozenset({"KEXEC", "KEXEC_FILE"})) in feat.gate_required  # either load syscall
    assert feat.enforcement is Enforcement.UPLOAD_REFUSAL


def test_only_the_named_gate_consumers_are_gated_and_the_rest_advertise_only():
    # #1848: this used to iterate six literal ids, so an advertise-only feature added later was
    # silently uncovered. Deriving both sets from the roster fixes that in both directions - a
    # new advertise-only feature is checked without editing the test, and a new *gated* feature
    # fails here on purpose. #1861 narrowed the set to crash_capture alone: gate.py imports
    # CRASH_CAPTURE and ROOTFS_MOUNT, and only crash-capture arming turns a refusal set into a
    # refusal (rootfs_mount is advertise-only and warns off the advertised clauses), so growing
    # the gated set is a decision, not a data addition.
    gated = {f.feature for f in FEATURE_REQUIREMENTS if f.gate_required}
    assert gated == {CRASH_CAPTURE}

    advertise_only = [f for f in FEATURE_REQUIREMENTS if f.feature not in gated]
    for feat in advertise_only:
        assert feat.gate_required == (), feat.feature
        assert feat.enforcement is not Enforcement.UPLOAD_REFUSAL, feat.feature
        # an advertise-only feature that advertises nothing would pass the two asserts above
        # while telling the agent nothing at all
        assert feat.advertised, feat.feature
        assert feat.summary.strip(), feat.feature


def test_sysrq_advertises_magic_sysrq_and_carries_no_refusal_set():
    # #1861: the entry used to carry gate_required=MAGIC_SYSRQ that no seam read, and `gated` was
    # derived from it, so feature_manifest() shipped `"gated": true` to an agent while the upload
    # path checked nothing. ADR-0318 decided sysrq is advertised and enforced by the runtime
    # detection in diagnostic_sysrq, so the refusal set is empty and the manifest says so.
    feat = feature_requirement(SYSRQ)
    assert feat.advertised == (Clause(frozenset({"MAGIC_SYSRQ"})),)
    assert feat.gate_required == ()
    entry = next(m for m in feature_manifest() if m["feature"] == SYSRQ)
    assert "refuses_on" not in entry
    # non-vacuity: the entry a reader gets must really advertise the symbol, otherwise an entry
    # with an empty requirements list would pass the asserts above while telling an agent
    # nothing about what to build
    assert entry["requirements"] == [{"symbols": ["MAGIC_SYSRQ"]}]


def test_sysrq_is_machine_readably_distinct_from_the_features_that_are_really_optional():
    # #1867, and the whole of it. #1861 dropped sysrq's unread refusal set, which was the honest
    # edit - but with a bare `gated` bool the entry then became *structurally identical* to kasan,
    # kcsan, kfence, kmemleak, lockdep, ftrace and kcov, which genuinely cost nothing to omit. An
    # agent that skips every `"gated": false` feature skipped MAGIC_SYSRQ and then paid a build,
    # install and boot before diagnostic_sysrq refused. That is the misreading, and this asserts
    # it is closed rather than asserting a new key exists: the two kinds of entry must differ on
    # a machine-readable value, and sysrq's must be the one that says a refusal is coming.
    manifest = {m["feature"]: m for m in feature_manifest()}
    sysrq = manifest[SYSRQ]
    assert sysrq["enforcement"] == Enforcement.RUNTIME_REFUSAL.value

    # The seven entries whose summaries really do say "omit this and you lose only the feature".
    # Spelled out rather than derived: the claim is that sysrq differs from *these*, and deriving
    # the comparison set from the same field under test would make it circular.
    for cheap in ("kasan", "kcsan", "kfence", "kmemleak", "lockdep", "ftrace", "kcov"):
        assert manifest[cheap]["enforcement"] == Enforcement.UNCHECKED.value, cheap
        assert manifest[cheap]["enforcement"] != sysrq["enforcement"], cheap

    # And the key that carried the misreading is gone, not merely joined. Leaving `gated` beside
    # `enforcement` would keep the cheaper-to-read field the one that groups sysrq with kasan.
    for entry in manifest.values():
        assert "gated" not in entry, entry["feature"]

    # A value an agent cannot look up is the same defect one layer along, so the vocabulary it
    # uses has to be defined in the payload the agent reads.
    legend = enforcement_legend()
    assert set(legend) == {e.value for e in Enforcement}
    assert sysrq["enforcement"] in legend
    # The served legend is the authored table verbatim - no renderer between them to drop a
    # definition or invent one.
    assert legend == {value.value: text for value, text in ENFORCEMENT_LEGEND.items()}
    # sysrq's definition must say the refusal is real and late, or the value is just a label.
    runtime = ENFORCEMENT_LEGEND[Enforcement.RUNTIME_REFUSAL].lower()
    assert "does not read your config" in runtime
    assert "refusal arrives when you invoke" in runtime
    assert "build, install and boot" in runtime
    # And "unchecked" must not be worded as a licence to skip, which would move the misreading one
    # value over rather than closing it: serial_console is unchecked and its VIRTIO_PCI clause is
    # boot-fatal, so the definition has to say what kdive does and send the reader to the summary
    # for what the omission costs.
    unchecked = ENFORCEMENT_LEGEND[Enforcement.UNCHECKED].lower()
    assert "not the same as optional" in unchecked
    assert "summary" in unchecked


def test_a_refusal_set_and_upload_refusal_cannot_drift_apart():
    # The #1861 defect class, made unrepresentable. `enforcement` is authored (the runtime_refusal
    # vs unchecked split is not derivable from any field), so nothing but this stops an entry from
    # claiming a refusal it has no set for, or carrying a set it does not admit to.
    for feat in FEATURE_REQUIREMENTS:
        refuses = feat.enforcement is Enforcement.UPLOAD_REFUSAL
        assert bool(feat.gate_required) is refuses, feat.feature

    # Both directions of the construction-time guard, so the check cannot be half-removed.
    with pytest.raises(ValueError, match="imply each other"):
        FeatureRequirement("x", "s", _CLAUSE, enforcement=Enforcement.UPLOAD_REFUSAL)
    with pytest.raises(ValueError, match="imply each other"):
        FeatureRequirement("x", "s", _CLAUSE, gate_required=_CLAUSE)
    # Non-vacuity: the two legal shapes really do construct, so the guard is not simply rejecting
    # every FeatureRequirement and passing both raises above by accident.
    assert FeatureRequirement("x", "s", _CLAUSE).enforcement is Enforcement.UNCHECKED
    assert (
        FeatureRequirement(
            "x", "s", _CLAUSE, gate_required=_CLAUSE, enforcement=Enforcement.UPLOAD_REFUSAL
        ).gate_required
        == _CLAUSE
    )


def test_manifest_covers_every_feature_and_exposes_advertised_not_gate_required():
    import json

    manifest = feature_manifest()
    assert {m["feature"] for m in manifest} == {f.feature for f in FEATURE_REQUIREMENTS}
    entry = next(m for m in manifest if m["feature"] == CRASH_CAPTURE)
    assert entry["enforcement"] == Enforcement.UPLOAD_REFUSAL.value
    assert entry["summary"]
    assert isinstance(entry["requirements"], list)
    # advertised superset carries KASLR (advertise-only); the gate-set exclusion is asserted above
    assert "RANDOMIZE_BASE" in json.dumps(entry["requirements"])
    assert "gate_required" not in entry  # the internal field name never reaches the document


def test_the_refusing_entry_publishes_which_of_its_advertised_clauses_can_refuse():
    # #1867's finer-grained half. `crash_capture` advertises seven clauses and refuses on five of
    # them; RANDOMIZE_BASE is ADR-0318's worked example of a symbol deliberately advertised and
    # never gated, so a ppc64le agent reading only `requirements` cannot tell the symbol it will
    # be refused over from the one that can never refuse. `refuses_on` is the answer, and it has
    # to be its own array rather than a flag on `requirements`: the advertised set splits {KEXEC}
    # and {KEXEC_FILE} where the gate joins them, so a per-clause flag would claim that omitting
    # KEXEC alone produces a refusal, which is false - either load syscall satisfies the gate.
    entry = next(m for m in feature_manifest() if m["feature"] == CRASH_CAPTURE)
    assert entry["refuses_on"] == [
        {"symbols": ["KEXEC", "KEXEC_FILE"]},  # one OR-group, not the two advertised clauses
        {"symbols": ["CRASH_DUMP"]},
        {"symbols": ["PROC_VMCORE"]},
        {"symbols": ["FW_CFG_SYSFS"], "arch": ["x86_64"]},
        {"symbols": ["RELOCATABLE"]},
    ]
    # The distinction the array exists to draw, asserted as a difference and not just a listing.
    refusing = _clause_symbols(entry["refuses_on"])
    advertised = _clause_symbols(entry["requirements"])
    assert "RANDOMIZE_BASE" in advertised
    assert "RANDOMIZE_BASE" not in refusing
    assert refusing < advertised  # a strict subset: advice is a superset of what can refuse

    # It is the only entry with one, and every entry that lacks one says why in `enforcement`.
    for other in feature_manifest():
        if other["feature"] == CRASH_CAPTURE:
            continue
        assert "refuses_on" not in other, other["feature"]
        assert other["enforcement"] != Enforcement.UPLOAD_REFUSAL.value, other["feature"]


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


def test_debuginfo_summary_sends_the_in_guest_drgn_reader_to_btf_rather_than_stopping_at_dwarf():
    # #1855 took DEBUG_INFO_BTF out of this entry's clause, which is right - a DWARF build is what
    # gdb and an offline vmcore need, and AND-ing BTF in would tell those readers otherwise. But
    # the summary's first sentence sells "live drgn" too, and in-guest drgn-live resolves from
    # /sys/kernel/btf, not from the .config's DWARF (the DWARF vmlinux is not on the guest rootfs -
    # gate.py says exactly this, and is why debuginfo_warning keys on BTF). With BTF gone from the
    # clause, the entry named it nowhere at all: an agent enabling DWARF5 for a live drgn session
    # satisfied every clause here and found out at debug.start_session, one build/install/boot
    # later. The clause stays DWARF-only; the summary carries the pointer.
    summary = feature_requirement("debuginfo").summary.lower()
    assert "debug_info_btf" in summary
    assert "/sys/kernel/btf" in summary
    # named as the other entry's, so the reader can find it rather than being left to search
    assert "bpf_tracing" in summary
    # and the consequence of stopping at DWARF, or the pointer reads as an optional extra
    assert "resolve a symbol" in summary
    # the escape hatch the seam itself offers, so the two surfaces agree
    assert "vmlinux" in summary
    # ...without the entry claiming BTF is required for what this feature IS for: the clause must
    # stay satisfiable by DWARF alone, which is the half #1855 settled and this prose may not
    # quietly undo.
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_DWARF5", "DEBUG_KERNEL"})
    assert unmet_advertised_clauses(cfg, feature_requirement("debuginfo")) == ()


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


def test_the_upload_enforcement_values_name_the_features_the_gate_module_really_reads():
    # ADR-0546 rule 1 claims the vocabulary is grounded in code rather than intent, and an
    # authored field can drift from the code it claims to describe - that is #1861 in one
    # sentence. gate.py is the only module that turns a FeatureRequirement into a payload, and it
    # reaches each one through feature_requirement(), so its call sites are the ground truth for
    # which entries may claim an upload_* value.
    #
    # Match the call sites, not the feature id anywhere in the text: "debuginfo" appears all over
    # gate.py (debuginfo_warning, MISSING_DEBUGINFO_REASON) while the `debuginfo` *entry* is read
    # by nothing - that seam keys on a module-level DEBUG_INFO_BTF literal on purpose (ADR-0544
    # §4). A substring search would fault a correct registry over it.
    import inspect
    import re

    from kdive.kernel_config import gate, requirements

    read = {
        getattr(requirements, name)
        for name in re.findall(r"feature_requirement\((\w+)\)", inspect.getsource(gate))
    }
    # Non-vacuity: an empty set would make the equality below hold against an all-`unchecked`
    # registry, and a renamed helper would empty it silently.
    assert read, "no feature_requirement() call site found in gate.py - the regex went stale"

    checked = {
        f.feature
        for f in FEATURE_REQUIREMENTS
        if f.enforcement in (Enforcement.UPLOAD_REFUSAL, Enforcement.UPLOAD_ADVISORY)
    }
    assert checked == read
    assert checked == {CRASH_CAPTURE, ROOTFS_MOUNT}
    # And the two are not interchangeable: only crash_capture turns its unmet set into a refusal.
    assert feature_requirement(CRASH_CAPTURE).enforcement is Enforcement.UPLOAD_REFUSAL
    assert feature_requirement(ROOTFS_MOUNT).enforcement is Enforcement.UPLOAD_ADVISORY


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
    # refusal rather than an upload check. #1861 removed the refusal set instead, and #1867
    # replaced the flag with one that states the late refusal itself, so the summary must not
    # reintroduce the explanation.
    assert "gated flag" not in summary
    assert feature_requirement(SYSRQ).gate_required == ()


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
    # #1876: an embedded-initramfs kernel (install.py:10/:301 - initrd_ref is None, no <initrd>
    # element) also has no <initrd> yet boots fine, the second case the uploaded-artifact
    # exception alone does not cover.
    assert "or the kernel embeds its own initramfs" in summary


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
    # #1876: an embedded-initramfs kernel (install.py:10/:301 - initrd_ref is None, no <initrd>
    # element) also has no <initrd> yet boots fine, the second case the uploaded-artifact
    # exception alone does not cover.
    assert "or the kernel embeds its own initramfs" in paren


def test_rootfs_mount_root_filesystem_is_an_or_group_not_two_and_clauses():
    # AND-of-OR: two _plain clauses would make every ext4-only local-libvirt kernel warn for a
    # missing XFS_FS (and vice versa). One OR-group keeps the advisory at "mounts nothing kdive
    # boots", which is the only claim kdive can make without a guest-family axis.
    # Read the symbol sets rather than whole clauses: the grouping is what this pins, and the
    # built-in value each clause also carries (#1860) is pinned by its own test below.
    feat = feature_requirement("rootfs_mount")
    grouping = [clause.symbols for clause in feat.advertised]
    assert frozenset({"EXT4_FS", "XFS_FS"}) in grouping
    assert frozenset({"EXT4_FS"}) not in grouping
    assert frozenset({"XFS_FS"}) not in grouping


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
    cfg = all_builtin(
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
    cfg = all_builtin(
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
        assert entry["enforcement"] == Enforcement.UNCHECKED.value, fid
        assert "refuses_on" not in entry, fid
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
        # lib/Kconfig.debug:398 DEBUG_INFO_BTF sits inside `if DEBUG_INFO` (:325-455) and selects
        # nothing, so it is settable only once a DWARF choice member is picked; #1855 keeps BTF as
        # the symbol named and AND-s that prerequisite in as its own OR-group.
        "bpf_tracing": (
            Clause(frozenset({"BPF_SYSCALL"})),
            Clause(frozenset({"PERF_EVENTS"})),
            Clause(frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"})),
            Clause(_DWARF_CHOICE),
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
    cfg = all_builtin(
        {
            "DEBUG_KERNEL",
            "FAULT_INJECTION",
            "FAILSLAB",
            "FAULT_INJECTION_DEBUG_FS",
            "DEBUG_FS",
            "SYSFS",
        }
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("fault_injection")) == ()


def test_a_configfs_only_fault_injection_kernel_is_told_it_still_needs_the_debugfs_interface():
    # FAULT_INJECTION_CONFIGFS looks like an alternative and is not: lib/fault-inject.c:188 gates
    # fault_create_debugfs_attr() - the only registration path FAILSLAB and friends use - on
    # CONFIG_FAULT_INJECTION_DEBUG_FS. Advertising the two as an OR-group would have called this
    # kernel complete while it exposes no knob to set a failure rate with.
    cfg = all_builtin(
        {
            "DEBUG_KERNEL",
            "FAULT_INJECTION",
            "FAILSLAB",
            "FAULT_INJECTION_CONFIGFS",
        }
    )
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("fault_injection")))
    assert missing == ["DEBUG_FS", "FAULT_INJECTION_DEBUG_FS", "SYSFS"]


def test_either_probe_event_source_satisfies_the_bpf_tracing_dependency():
    # kernel/trace/Kconfig:855: BPF_EVENTS needs KPROBE_EVENTS *or* UPROBE_EVENTS, so a
    # uprobe-only kernel is complete and two AND clauses would falsely fault it. The config also
    # carries a DWARF choice member because #1855 AND-ed that prerequisite in beside BTF; without
    # it this kernel is incomplete for a reason that has nothing to do with probe-event sources.
    cfg = all_builtin(
        {
            "BPF_SYSCALL",
            "PERF_EVENTS",
            "UPROBE_EVENTS",
            "DEBUG_INFO",
            "DEBUG_INFO_DWARF5",
            "DEBUG_INFO_BTF",
        }
    )
    assert unmet_advertised_clauses(cfg, feature_requirement("bpf_tracing")) == ()


def _unsettable_symbols_named(features: tuple[FeatureRequirement, ...]) -> dict[str, str]:
    """Offending symbols in any clause of any feature, mapped to the Kconfig line each was read at.

    Walks ``advertised`` and ``gate_required`` alike: the second is where #1854's defect lived,
    and it is the half a symbol reaches the agent through a *refusal* rather than an advisory.
    """
    return {
        symbol: UNSETTABLE_SYMBOLS[symbol]
        for f in features
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        for symbol in clause.symbols
        if symbol in UNSETTABLE_SYMBOLS
    }


def test_no_clause_of_any_feature_names_a_symbol_the_agent_cannot_set():
    # Invariant I1 of the clause model #1854 settles. A prompt-less or auto-select-ed symbol
    # cannot be set from a config fragment - olddefconfig drops the line - so reporting one as
    # missing sends the agent after the one thing it cannot do. #1850 held this over `advertised`
    # only, which let crash_capture keep KEXEC_CORE and VMCORE_INFO in `gate_required`. The rule
    # bars them from every clause, so the walk above reads both fields.
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
    # The six are spelled here rather than imported beside the list they bound: a tripwire in the
    # same file as the thing it guards is defeated by one hunk. Deleting a row as "not named
    # anywhere anyway" has to be done in three files across two trees, where a reviewer sees it.
    named_unsettable_by_the_registry = {
        "KEXEC_CORE",
        "VMCORE_INFO",
        "DEBUG_INFO",
        "BPF_EVENTS",
        "TRACING",
        "DYNAMIC_FTRACE",
    }
    assert set(UNSETTABLE_SYMBOLS) >= named_unsettable_by_the_registry, sorted(
        named_unsettable_by_the_registry - set(UNSETTABLE_SYMBOLS)
    )
    # non-vacuity: the walk must actually see the roster
    assert _every_clause_symbol(FEATURE_REQUIREMENTS)
    # The failure message names the Kconfig file:line of every offender so a new entry can be
    # checked against the kernel directly.
    assert _unsettable_symbols_named(FEATURE_REQUIREMENTS) == {}


def _every_clause_symbol(features: tuple[FeatureRequirement, ...]) -> set[str]:
    return {
        symbol
        for f in features
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        for symbol in clause.symbols
    }


def test_the_invariant_above_reaches_the_refusal_set_and_not_only_the_advertised_one():
    # The half #1850's version could not have caught, and the half nothing else here proves.
    # Every symbol crash_capture gates is also advertised, so narrowing the check to `advertised`
    # would leave the invariant above green against the live roster while the refusal set - where
    # #1854's defect lived - went unwatched. Feed the same function a feature that names an
    # unsettable symbol in gate_required ONLY, and require it to be reported.
    smuggled = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the invariant above",
        advertised=(Clause(frozenset({"KEXEC"})),),
        gate_required=(Clause(frozenset({"KEXEC_CORE"})),),
        enforcement=Enforcement.UPLOAD_REFUSAL,
    )
    assert _unsettable_symbols_named((smuggled,)) == {"KEXEC_CORE": "kernel/Kconfig.kexec:11"}
    # and the advertised half is still read, so neither field can be dropped unnoticed
    assert _every_clause_symbol((smuggled,)) == {"KEXEC", "KEXEC_CORE"}


def test_debuginfo_advertises_the_dwarf_choice_members_and_only_those():
    # #1855, narrowing #1850. lib/Kconfig.debug:249 DEBUG_INFO is a bare prompt-less bool, so it
    # stays unadvertised; what advertises it is the `choice` at :262-323, whose three non-`NONE`
    # members - DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT (:281), DEBUG_INFO_DWARF4 (:293),
    # DEBUG_INFO_DWARF5 (:305) - each select DEBUG_INFO (:283, :295, :307) and yield real DWARF.
    # The toolchain-default member was missing, so a kernel built on it carried full debug info and
    # was still reported as missing all three advertised symbols.
    #
    # DEBUG_INFO_BTF (:398) leaves this group. It is not a choice member at all - it sits inside
    # `if DEBUG_INFO` (:325-455) and selects nothing - so it was an alternative the agent could not
    # take from a bare config, and it is not what this feature is for either: an offline vmcore and
    # gdb need DWARF, which is why it keeps its home under bpf_tracing instead.
    feat = feature_requirement("debuginfo")
    assert feat.advertised == (
        Clause(_DWARF_CHOICE),
        Clause(frozenset({"DEBUG_KERNEL"})),
    )


def test_a_toolchain_default_kernel_is_no_longer_reported_as_missing_every_debuginfo_symbol():
    # The false positive #1855 reports, stated as the config that exposes it. A kernel that took
    # the choice's default carries DWARF that drgn and gdb read, and it advertised none of the
    # three symbols the old group named - so the advisory told a complete debuginfo kernel to
    # rebuild.
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT", "DEBUG_KERNEL"})
    assert unmet_advertised_clauses(cfg, feature_requirement("debuginfo")) == ()


def test_a_btf_only_kernel_is_told_to_pick_a_dwarf_member_rather_than_called_complete():
    # The other half of #1855: BTF was offered as a third alternative to DWARF4/DWARF5, so a config
    # carrying BTF alone read as a complete debuginfo build. It is not one - BTF is a compressed
    # type description, not the line and location tables an offline vmcore or gdb session needs -
    # and the path to it is not reachable from a bare config either. Naming the three DWARF members
    # is the advice that works in both directions.
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_BTF", "DEBUG_KERNEL"})
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("debuginfo")))
    assert missing == [
        "DEBUG_INFO_DWARF4",
        "DEBUG_INFO_DWARF5",
        "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
    ]


def test_a_kernel_with_no_debug_info_is_still_told_which_settable_symbols_to_build_in():
    # Non-vacuity guard for the two tests above: neither the #1850 removal of DEBUG_INFO nor the
    # #1855 removal of DEBUG_INFO_BTF may make the advisory quieter on the kernel it exists to
    # catch. It stays the same length, naming one more DWARF member and one fewer BTF.
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("debuginfo")))
    assert missing == [
        "DEBUG_INFO_DWARF4",
        "DEBUG_INFO_DWARF5",
        "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
        "DEBUG_KERNEL",
    ]


def test_bpf_tracing_names_the_same_dwarf_choice_debuginfo_advertises():
    # #1855 puts the same three symbols in two features for two different reasons - debuginfo
    # advertises them as the feature itself, bpf_tracing carries them as the prerequisite BTF is
    # settable behind - and a reader of either has to be given the same three names. Pinned as an
    # equality between the two live clauses so that adding a fourth choice member to one entry and
    # not the other fails here rather than in whichever seam reads the stale copy.
    dwarf_clause = Clause(_DWARF_CHOICE)
    assert dwarf_clause in feature_requirement("debuginfo").advertised
    assert dwarf_clause in feature_requirement("bpf_tracing").advertised


def test_bpf_tracing_orders_the_dwarf_prerequisite_before_the_btf_symbol_it_gates():
    # The advertised tuple is ordered, and an agent reads it top to bottom: "pick a DWARF member,
    # then BTF" is followable, the reverse is the ordering that made #1855's BTF-only advice a dead
    # end in the first place.
    advertised = feature_requirement("bpf_tracing").advertised
    assert advertised.index(Clause(_DWARF_CHOICE)) < advertised.index(
        Clause(frozenset({"DEBUG_INFO_BTF"}))
    )


def test_a_bpf_kernel_with_no_debug_info_is_told_to_pick_a_dwarf_member_as_well_as_btf():
    # The bite for the AND-ed prerequisite. kernel/trace/Kconfig BTF sits inside `if DEBUG_INFO`
    # and selects nothing, so CONFIG_DEBUG_INFO_BTF=y in a fragment over a bare config is dropped
    # by olddefconfig and the agent gets a kernel with no BTF and no error. The advisory now names
    # the DWARF member that has to come first, instead of only the symbol that will be discarded.
    cfg = all_builtin({"BPF_SYSCALL", "PERF_EVENTS", "UPROBE_EVENTS"})
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("bpf_tracing")))
    assert missing == [
        "DEBUG_INFO_BTF",
        "DEBUG_INFO_DWARF4",
        "DEBUG_INFO_DWARF5",
        "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
    ]


def test_a_complete_bpf_tracing_kernel_on_any_dwarf_member_draws_no_advisory():
    # The inverse, over each member in turn: the prerequisite clause is an OR-group, so a kernel
    # that picked any one of the three is complete. Three AND clauses here would fault every real
    # BPF kernel for the two DWARF members it did not pick.
    for member in sorted(_DWARF_CHOICE):
        cfg = all_builtin(
            {
                "BPF_SYSCALL",
                "PERF_EVENTS",
                "UPROBE_EVENTS",
                "DEBUG_INFO_BTF",
                "DEBUG_INFO",
                member,
            }
        )
        assert unmet_advertised_clauses(cfg, feature_requirement("bpf_tracing")) == (), member


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
    # On a modern kernel the pair carries no signal the surviving clauses do not: KEXEC and
    # KEXEC_FILE select KEXEC_CORE, CRASH_DUMP selects VMCORE_INFO, and each of those selectors
    # is itself a clause here, so a coherent .config carrying a selector carries what it selects.
    # Where the pair did carry signal it was wrong - see the pre-6.9 kernel below.
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


def test_a_pre_6_9_kernel_that_has_no_vmcore_info_symbol_at_all_is_armed_rather_than_refused():
    # The payload assertion #1854 asks for, stated as the case the old refusal got outright
    # wrong. VMCORE_INFO was split out of CRASH_CORE in Linux 6.9, so it is not a symbol a
    # pre-6.9 kernel's .config can carry at any setting. That kernel captures a vmcore perfectly
    # well, and its complete, coherent config drew a refusal naming a symbol absent from its own
    # Kconfig - unfixable by any rebuild, which is what made the remediation a dead end.
    cfg = all_builtin(
        {
            "KEXEC",
            "KEXEC_CORE",
            "KEXEC_FILE",
            "CRASH_DUMP",
            "PROC_VMCORE",
            "FW_CFG_SYSFS",
            "RELOCATABLE",
        }
    )
    assert unmet_clauses(cfg, feature_requirement(CRASH_CAPTURE), arch=_X86) == ()


def test_a_hand_truncated_config_missing_a_derived_symbol_is_armed_rather_than_refused():
    # The second class the removal newly arms, and the one it trades away: a .config edited or
    # truncated so a selector is present without the symbol it selects. Arming it is the
    # fail-open direction ADR-0318 already chose for a config kdive cannot correlate with a
    # kernel, and it is asserted here so the trade is visible rather than incidental.
    cfg = all_builtin(
        {"KEXEC", "KEXEC_FILE", "CRASH_DUMP", "PROC_VMCORE", "FW_CFG_SYSFS", "RELOCATABLE"}
    )
    assert unmet_clauses(cfg, feature_requirement(CRASH_CAPTURE), arch=_X86) == ()


def test_a_kernel_that_genuinely_cannot_kexec_is_still_refused_and_named():
    # The other direction, so the removals above cannot be read as "the gate got quieter". A
    # kernel that really cannot kexec is refused exactly as before, and both symbols it names are
    # ones a config fragment can set - which is what makes the gate's "rebuild with the missing
    # CONFIG_*" remediation followable rather than a dead end.
    cfg = all_builtin({"KEXEC", "KEXEC_FILE", "CRASH_DUMP", "RELOCATABLE"})
    missing = missing_symbols(unmet_clauses(cfg, feature_requirement(CRASH_CAPTURE), arch=_X86))
    assert missing == ["FW_CFG_SYSFS", "PROC_VMCORE"]


def test_a_kernel_with_no_kexec_at_all_is_still_told_which_settable_symbols_to_build_in():
    # Non-vacuity guard for the crash_capture removal: the advisory on a bare kernel must still
    # name every symbol the agent can act on, minus the two it cannot.
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    feature = feature_requirement(CRASH_CAPTURE)
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature, arch=_X86))
    assert missing == [
        "CRASH_DUMP",
        "FW_CFG_SYSFS",
        "KEXEC",
        "KEXEC_FILE",
        "PROC_VMCORE",
        "RANDOMIZE_BASE",
        "RELOCATABLE",
    ]
    # and the ppc64le agent is told the subset it can actually act on, rather than two symbols
    # its Kconfig does not offer at any setting (#1875) - the advice half of the same defect the
    # gated clause carries. The list shrinks by exactly the two x86-only symbols and no more.
    assert missing_symbols(unmet_advertised_clauses(cfg, feature, arch=_PPC)) == [
        "CRASH_DUMP",
        "KEXEC",
        "KEXEC_FILE",
        "PROC_VMCORE",
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
        cfg = all_builtin({"KASAN", mode, "STACKTRACE"})
        assert unmet_advertised_clauses(cfg, feature_requirement("kasan")) == (), mode


def test_a_kernel_with_no_sanitizer_at_all_is_told_what_kasan_needs():
    # Non-vacuity guard for the test above: the same check must still report on a bare kernel.
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    missing = missing_symbols(unmet_advertised_clauses(cfg, feature_requirement("kasan")))
    assert missing == ["KASAN", "KASAN_GENERIC", "KASAN_HW_TAGS", "KASAN_SW_TAGS", "STACKTRACE"]


def test_the_boot_clauses_require_a_built_in_unless_the_build_uploaded_an_initrd():
    # #1860. Both rootfs_mount clauses: the direct-kernel boot mounts root before any module can
    # load, so EXT4_FS=m / XFS_FS=m / VIRTIO_BLK=m is a kernel that panics on an unmountable root.
    # UNLESS_INITRD and not REQUIRED because an uploaded initrd is exactly what supplies the
    # modules - the relaxation SERIAL_8250's and IKCONFIG's Kconfig-level =y does not get.
    feat = feature_requirement(ROOTFS_MOUNT)
    assert {clause.built_in for clause in feat.advertised} == {BuiltIn.UNLESS_INITRD}
    assert len(feat.advertised) == 2  # non-vacuity: an empty tuple would satisfy the set above


def test_the_virtio_transport_is_boot_critical_and_the_console_symbol_is_not():
    # The other half of #1860's subject. VIRTIO_PCI is a tristate whose own Kconfig help says "If
    # unsure, say M", and it is the transport the rootfs_mount virtio-blk disk binds through - so a
    # modular one loses the root disk for the same reason. SERIAL_8250_CONSOLE is a bool with no
    # module form and takes no value here; the SERIAL_8250 prerequisite it depends on is #1859's.
    by_symbols = {
        clause.symbols: clause.built_in
        for clause in feature_requirement("serial_console").advertised
    }
    assert by_symbols[frozenset({"VIRTIO_PCI"})] is BuiltIn.UNLESS_INITRD
    assert by_symbols[frozenset({"SERIAL_8250_CONSOLE"})] is BuiltIn.NOT_REQUIRED


def test_ikconfig_needs_a_built_in_that_no_initrd_relieves():
    # Beyond #1860's literal text and the same defect class: /proc/config.gz is created by the
    # IKCONFIG module's own init and disappears with it, so CONFIG_IKCONFIG=m does not deliver the
    # readback at any point - there is no boot-ordering window an initrd could widen. REQUIRED is
    # what says that; UNLESS_INITRD would wrongly call a modular ikconfig fine on an initrd build.
    by_symbols = {
        clause.symbols: clause.built_in for clause in feature_requirement("ikconfig").advertised
    }
    assert by_symbols[frozenset({"IKCONFIG"})] is BuiltIn.REQUIRED
    # IKCONFIG_PROC is a bool with no module form, so it needs no value and must not gain one by
    # a blanket sweep over the entry.
    assert by_symbols[frozenset({"IKCONFIG_PROC"})] is BuiltIn.NOT_REQUIRED


def test_no_other_feature_gained_a_built_in_requirement():
    # The sweep is exactly three entries. A blanket application would break the reason #1860 did
    # not narrow the parse regex: KASAN, ftrace, kcov and the BPF symbols are the feature the agent
    # asked for at =m, and marking them would fault every modular sanitizer build.
    marked = {
        f.feature
        for f in FEATURE_REQUIREMENTS
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        if clause.built_in is not BuiltIn.NOT_REQUIRED
    }
    assert marked == {ROOTFS_MOUNT, "serial_console", "ikconfig"}
    for feature_id in ("kasan", "ftrace", "kcov", "bpf_tracing"):
        feat = feature_requirement(feature_id)
        assert {clause.built_in for clause in feat.advertised} == {BuiltIn.NOT_REQUIRED}, feature_id


def test_the_manifest_clause_object_carries_the_built_in_value_and_omits_it_at_the_default():
    # The element is an object (#1854), so the value lands as a key beside `symbols`. It is omitted
    # at NOT_REQUIRED so an unconstrained clause stays as small as it was and the document does not
    # grow a key on every one of the dozens of clauses that do not need it.
    by_feature = {entry["feature"]: entry["requirements"] for entry in feature_manifest()}
    rootfs = by_feature[ROOTFS_MOUNT]
    assert isinstance(rootfs, list)
    assert {"symbols": ["EXT4_FS", "XFS_FS"], "built_in": "unless_initrd"} in rootfs
    assert {"symbols": ["VIRTIO_BLK"], "built_in": "unless_initrd"} in rootfs
    ikconfig = by_feature["ikconfig"]
    assert isinstance(ikconfig, list)
    assert {"symbols": ["IKCONFIG"], "built_in": "required"} in ikconfig
    # the default renders as a bare `symbols` object, with no key at all
    assert {"symbols": ["IKCONFIG_PROC"]} in ikconfig
    kcov = by_feature["kcov"]
    assert kcov == [{"symbols": ["KCOV"]}]


_X86 = "x86_64"
_PPC = "ppc64le"


def test_serial_console_advertises_a_console_for_each_arch_and_the_8250_prerequisite():
    # #1859, the clause content ADR-0544 §3 settles. The old set was two clauses,
    # {SERIAL_8250_CONSOLE} and {VIRTIO_PCI}, and both of the issue's defects lived in the first:
    #
    #   - SERIAL_8250_CONSOLE alone is not settable. drivers/tty/serial/8250/Kconfig:70-72 makes
    #     it a bool "depends on SERIAL_8250=y", so a fragment setting only it is dropped by
    #     olddefconfig. Under rule 1 the prerequisite becomes its own clause rather than replacing
    #     the symbol - and it is REQUIRED, the Kconfig-level =y no initrd relieves.
    #   - a ppc64le guest was advertised the wrong symbol and never told the right one. Its
    #     console is hvc0, driven by HVC_CONSOLE (drivers/tty/hvc/Kconfig:14, "depends on
    #     PPC_PSERIES"), on which SERIAL_8250_CONSOLE does nothing.
    #
    # Asserts the clause *tuple*, not the flattened union: an OR-group
    # {SERIAL_8250_CONSOLE, HVC_CONSOLE} - the shape the issue names as fitting the model without
    # an arch axis, and ADR-0544 rejects - has the same union and would slip past a set compare.
    assert feature_requirement("serial_console").advertised == (
        Clause(frozenset({"SERIAL_8250"}), BuiltIn.REQUIRED, arches=frozenset({_X86})),
        Clause(frozenset({"SERIAL_8250_CONSOLE"}), arches=frozenset({_X86})),
        Clause(frozenset({"HVC_CONSOLE"}), arches=frozenset({_PPC})),
        Clause(frozenset({"VIRTIO_PCI"}), BuiltIn.UNLESS_INITRD),
    )


def test_the_8250_prerequisite_is_ordered_before_the_console_symbol_it_gates():
    # Same ordering rule bpf_tracing's DWARF-before-BTF clause follows: the advertised tuple is
    # rendered in order into the contract document, so "set SERIAL_8250, then
    # SERIAL_8250_CONSOLE" has to read in the order the two have to be done.
    symbols = [
        sorted(clause.symbols) for clause in feature_requirement("serial_console").advertised
    ]
    assert symbols.index(["SERIAL_8250"]) < symbols.index(["SERIAL_8250_CONSOLE"])


def test_the_two_console_symbols_are_separate_arch_scoped_clauses_not_one_or_group():
    # The direction of error ADR-0544 rejects the OR-group on: an x86 kernel carrying only
    # HVC_CONSOLE would read as satisfied, and a ppc64le kernel carrying only SERIAL_8250_CONSOLE
    # likewise. Stated as the configs rather than the shape, so it holds whatever the clauses look
    # like. The union is identical either way, which is why a set comparison could not see this.
    feature = feature_requirement("serial_console")
    hvc_only_on_x86 = all_builtin({"HVC_CONSOLE", "VIRTIO_PCI"})
    assert missing_symbols(unmet_advertised_clauses(hvc_only_on_x86, feature, arch=_X86)) == [
        "SERIAL_8250",
        "SERIAL_8250_CONSOLE",
    ]
    eight250_only_on_ppc = all_builtin({"SERIAL_8250", "SERIAL_8250_CONSOLE", "VIRTIO_PCI"})
    assert missing_symbols(unmet_advertised_clauses(eight250_only_on_ppc, feature, arch=_PPC)) == [
        "HVC_CONSOLE"
    ]


def test_a_complete_kernel_of_each_arch_shape_draws_no_serial_console_advisory():
    # The both-directions non-vacuity guard the issue asks for, first direction: a kernel that is
    # complete for its own arch is silent, and is not faulted for the other arch's console.
    feature = feature_requirement("serial_console")
    x86 = all_builtin({"SERIAL_8250", "SERIAL_8250_CONSOLE", "VIRTIO_PCI"})
    assert unmet_advertised_clauses(x86, feature, arch=_X86) == ()
    ppc = all_builtin({"HVC_CONSOLE", "VIRTIO_PCI"})
    assert unmet_advertised_clauses(ppc, feature, arch=_PPC) == ()


def test_a_bare_kernel_is_still_told_every_symbol_it_can_act_on_for_its_own_arch():
    # Second direction of the same guard: the skip must not make the advisory silent on a kernel
    # that really is missing its console. A ppc64le kernel with no console at all was reported
    # nothing before #1859 - that is the issue's second defect, stated as the payload.
    feature = feature_requirement("serial_console")
    bare = all_builtin({"EXT4_FS"})
    assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=_PPC)) == [
        "HVC_CONSOLE",
        "VIRTIO_PCI",
    ]
    assert missing_symbols(unmet_advertised_clauses(bare, feature, arch=_X86)) == [
        "SERIAL_8250",
        "SERIAL_8250_CONSOLE",
        "VIRTIO_PCI",
    ]
    # and with no arch in hand, only the unscoped clause is answerable - kdive never invents a
    # requirement it cannot establish (ADR-0544 §3)
    assert missing_symbols(unmet_advertised_clauses(bare, feature)) == ["VIRTIO_PCI"]


def test_no_other_feature_gained_an_arch_scope():
    # The sweep is exactly two entries, and it has to stay that way: invariant I2 below is what
    # forbids tagging rootfs_mount (complete_build still resolves no arch), and this is what
    # notices a third feature being tagged for a reason neither check covers.
    scoped = {
        f.feature
        for f in FEATURE_REQUIREMENTS
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        if clause.arches is not None
    }
    assert scoped == {"serial_console", CRASH_CAPTURE}


def test_crash_capture_scopes_the_two_symbols_no_ppc64le_kernel_can_set_and_no_others():
    # ADR-0544 §7's residual, closed by #1875 and inverted into the positive assertion. Verified
    # against upstream Linux v7.0 (3131ff5a117498bb4b9db3a238bb311cbf8383ce), symbol by symbol:
    #
    #   FW_CFG_SYSFS  drivers/firmware/Kconfig:122 - its only powerpc dependency arm is PPC_PMAC,
    #                 and PPC_PMAC itself depends on CPU_BIG_ENDIAN, so no ppc64le kernel offers
    #                 it at any machine type. Gated AND advertised.
    #   RANDOMIZE_BASE  arch/powerpc/Kconfig:688 - `depends on PPC_85xx && FLATMEM`, 32-bit e500.
    #                 Advertised only (ADR-0318's deliberately-ungated KASLR symbol).
    #   RELOCATABLE   arch/powerpc/Kconfig:665 - `depends on PPC64 || ...`, a real prompt on
    #                 ppc64le, so it stays unscoped and keeps refusing on both arches.
    #
    # Pinned as an exact per-symbol map rather than a set of scoped names, so widening the scope
    # to RELOCATABLE - which would silently stop refusing a ppc64le kernel that cannot relocate -
    # reddens here rather than passing as "still two symbols tagged".
    x86_only = frozenset({_X86})  # spelled independently of the registry's own constant
    feat = feature_requirement(CRASH_CAPTURE)
    gated = {symbol: clause.arches for clause in feat.gate_required for symbol in clause.symbols}
    assert gated == {
        "KEXEC": None,
        "KEXEC_FILE": None,
        "CRASH_DUMP": None,
        "PROC_VMCORE": None,
        "FW_CFG_SYSFS": x86_only,
        "RELOCATABLE": None,
    }
    advertised = {symbol: clause.arches for clause in feat.advertised for symbol in clause.symbols}
    assert advertised == {**gated, "RANDOMIZE_BASE": x86_only}


def test_a_third_provisionable_arch_forces_the_crash_capture_scope_to_be_re_verified():
    # The converse guard for crash_capture, and the one a subset assertion cannot give. An
    # `arches` value is an ALLOW-list and the support checks SKIP a clause outside it, so a scope
    # that names fewer arches than kdive provisions is always accepted by
    # test_every_arch_scope_names_an_arch_kdive_can_provision - `{x86_64} <= {x86_64, ppc64le,
    # aarch64}` holds. That is the same hole
    # test_every_arch_kdive_can_provision_is_advertised_a_console closes for serial_console.
    #
    # It matters here because FW_CFG_SYSFS is not x86-specific in the kernel: its dependency arm
    # is `ARM || ARM64 || PARISC || PPC_PMAC || RISCV || SPARC || X86`, so it is settable - and
    # needed for a QEMU guest's crash capture - on nearly every arch kdive might add next.
    # Adding one to `_TRAITS` would silently drop it out of that arch's refusal set with the whole
    # suite green. Reddening here forces whoever adds the arch to re-read the Kconfig and either
    # widen the scope or state why the arch stays out.
    scoped = {
        symbol: clause.arches
        for f in FEATURE_REQUIREMENTS
        if f.feature == CRASH_CAPTURE
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        if clause.arches is not None
        for symbol in clause.symbols
    }
    assert scoped  # non-vacuity: an empty map would make every loop below pass
    for symbol, arches in scoped.items():
        assert SUPPORTED_ARCHES - (arches or frozenset()) == {_PPC}, (
            f"crash_capture scopes {symbol} to {sorted(arches or ())}, leaving "
            f"{sorted(SUPPORTED_ARCHES - (arches or frozenset()))} out of its checks. Only "
            "ppc64le was verified against upstream Kconfig as unable to set it (#1875); re-read "
            "the symbol's `depends on` for the new arch before leaving it out."
        )
    # and the check discriminates: a scope that omitted a second arch would be reported
    assert SUPPORTED_ARCHES - frozenset({_X86, _PPC}) != {_PPC}


def test_every_arch_scope_names_an_arch_kdive_can_provision():
    # ADR-0544 §3's third test. A clause scoped to an arch kdive cannot boot is a requirement no
    # kernel is ever checked against - it would sit in the contract document advertising a symbol
    # to nobody, and the support checks would skip it forever. Read from the test tree only, so
    # `kernel_config` keeps taking no runtime import on `domain.platform`.
    scopes = [
        clause.arches
        for f in FEATURE_REQUIREMENTS
        for clauses in (f.advertised, f.gate_required)
        for clause in clauses
        if clause.arches is not None
    ]
    assert scopes  # non-vacuity: an empty list is a subset of anything
    for arches in scopes:
        assert arches  # an empty frozenset would mean "no arch", which None does not say
        assert arches <= SUPPORTED_ARCHES, sorted(arches)
    # and the check is really discriminating, not a subset test against a set of everything
    assert not frozenset({"riscv64"}) <= SUPPORTED_ARCHES


def _importers_of_support() -> set[str]:
    """Files under ``src/kdive`` importing ``kernel_config.support``, as repo-relative paths.

    Kept here rather than in test_layering.py because the premise it protects is I2's, and I2
    lives in this module.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    target = "kdive.kernel_config.support"
    importers: set[str] = set()
    scanned = sorted(src.rglob("*.py"))
    assert scanned, f"no modules found under {src}; the walk below would pass vacuously"
    for path in scanned:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == target or name.startswith(f"{target}.") for name in names):
                importers.add(path.relative_to(src).as_posix())
    return importers


def test_every_arch_kdive_can_provision_is_advertised_a_console():
    # The converse of the test above, and the one that guards #1859's actual defect. That test
    # stops a clause naming an arch kdive cannot boot; this one stops an arch kdive CAN boot from
    # being advertised nothing - which is the half #1859 reported ("advertises nothing at all for
    # ppc64le"). Without it, adding a third arch to `_TRAITS` reintroduces the defect with the
    # whole suite green, because a subset assertion is satisfied by advertising fewer arches, not
    # more.
    #
    # `console_device` is the source of truth these tags duplicate by hand: a guest consoles on
    # whatever the trait names, so an arch with a console device and no clause offering a driver
    # for it is an agent told nothing about the one symbol its boot depends on.
    consoles = [
        clause
        for clause in feature_requirement("serial_console").advertised
        if clause.arches is not None
    ]
    assert consoles  # non-vacuity: no scoped clause at all would pass the loop below
    covered = {arch for clause in consoles for arch in clause.arches or frozenset()}
    assert covered == SUPPORTED_ARCHES, (
        "serial_console advertises a console driver for "
        f"{sorted(covered)} but kdive provisions {sorted(SUPPORTED_ARCHES)}; an arch with a "
        "console_device and no clause is the defect #1859 reported"
    )
    # and every one of those arches really does have a console device to drive - the tag is not
    # covering an arch that consoles some other way
    assert all(arch_traits(arch).console_device for arch in covered)


def test_only_the_gate_reads_the_support_checks():
    # I2 discovers seams by AST-walking gate.py alone, so a seam calling unmet_clauses from
    # anywhere else is invisible to it. That matters more for the arch axis than the initrd one:
    # an unsupplied `has_initrd` defaults strict and over-reports, but an unsupplied `arch` SKIPS
    # the clause, so an arch-scoped gated clause read from an undiscovered seam would vanish out
    # of a refusal set silently. This keeps the AST walk's premise true rather than assumed.
    importers = _importers_of_support()
    assert importers, "nothing imports kernel_config.support, so this guard proves nothing"
    assert importers == {"kdive/kernel_config/gate.py"}, (
        f"kernel_config.support is imported outside gate.py by {sorted(importers)}; I2's seam "
        "discovery only walks gate.py, so teach it that seam before adding this import"
    )


def test_the_manifest_clause_object_carries_the_arch_scope_and_omits_it_at_the_default():
    # ADR-0544 §6: the arch lands as a key beside `symbols`, sorted for a diffable document, and
    # omitted at the None default so the dozens of unscoped clauses stay as small as they were.
    # The machine-readable array is what an agent diffs its config against, so a ppc64le agent
    # filtering on `arch` is the whole point of the field.
    by_feature = {entry["feature"]: entry["requirements"] for entry in feature_manifest()}
    console = by_feature["serial_console"]
    assert console == [
        {"symbols": ["SERIAL_8250"], "built_in": "required", "arch": [_X86]},
        {"symbols": ["SERIAL_8250_CONSOLE"], "arch": [_X86]},
        {"symbols": ["HVC_CONSOLE"], "arch": [_PPC]},
        {"symbols": ["VIRTIO_PCI"], "built_in": "unless_initrd"},
    ]
    # crash_capture is the second scoped entry (#1875): the two symbols no ppc64le kernel can set
    # carry the key, the five that are settable on both arches do not, so a ppc64le agent
    # filtering this array is told exactly what it can act on.
    assert by_feature[CRASH_CAPTURE] == [
        {"symbols": ["KEXEC"]},
        {"symbols": ["KEXEC_FILE"]},
        {"symbols": ["CRASH_DUMP"]},
        {"symbols": ["PROC_VMCORE"]},
        {"symbols": ["FW_CFG_SYSFS"], "arch": [_X86]},
        {"symbols": ["RELOCATABLE"]},
        {"symbols": ["RANDOMIZE_BASE"], "arch": [_X86]},
    ]
    # the default renders as an object with no `arch` key at all, on every other feature
    others = [
        clause
        for feature, clauses in by_feature.items()
        if feature not in {"serial_console", CRASH_CAPTURE} and isinstance(clauses, list)
        for clause in clauses
    ]
    assert others  # non-vacuity: the rest of the roster is really walked
    assert all(isinstance(clause, dict) and "arch" not in clause for clause in others)


class SeamFacts(NamedTuple):
    """Which conditional facts EVERY seam evaluating a feature passes to the support checks.

    One row per axis rather than one map per axis: I2 is a single invariant over three axes
    (ADR-0544 §7, ADR-0545), and three hand-maintained maps keyed by the same feature ids would be
    three things to keep in step with gate.py instead of one.
    """

    initrd: bool
    """Whether every such seam passes ``has_initrd``, one of the two ``UNLESS_INITRD`` reliefs."""

    guest_initramfs: bool
    """Whether every such seam passes ``guest_builds_initramfs``, the other one (ADR-0545)."""

    arch: bool
    """Whether every such seam passes ``arch``, which an arch-scoped clause needs."""


# Features a seam resolves and evaluates clauses of, mapped to the facts EVERY such seam supplies.
# HAND-MAINTAINED, and pinned to gate.py by the test below so a feature wired into a seam cannot
# be left off it silently.
_SEAM_SUPPLIES: Final[MappingProxyType[str, SeamFacts]] = MappingProxyType(
    {
        # crash_capture_refusal holds no BuildStepResult: the install and vmcore seams reach it
        # with a Run id and nothing about the build's artifacts. The boot-model half is a weaker
        # False - both call sites do have a Run in scope (vmcore/handlers.py loads one to
        # authorize before calling; install.py's _validate_crashkernel sits a frame below one), so
        # that axis is unwired rather than unreachable. This row records what the seam PASSES,
        # which is nothing on either initrd axis.
        #
        # The arch axis is True as of #1875: both seams already hold the Run's System and now read
        # `system_arch(system)` off its provisioning profile, and crash_capture_refusal takes the
        # keyword WITHOUT a default, so a third seam that cannot resolve one is a type error
        # rather than a refusal that silently skips its scoped clause (ADR-0544 §7, ADR-0545).
        CRASH_CAPTURE: SeamFacts(initrd=False, guest_initramfs=False, arch=True),
        # rootfs_mount_warning takes both reliefs, and its one caller (_success_envelope in
        # mcp/tools/lifecycle/runs/complete_build.py) supplies both off values it already holds:
        # `result.initrd_ref is not None` from the finalized BuildStepResult, and the boot model
        # from the Run's own target_kind (ADR-0545). It holds no arch: complete_build runs against
        # a Run, and nothing on that path resolves the System profile it will install to.
        ROOTFS_MOUNT: SeamFacts(initrd=True, guest_initramfs=True, arch=False),
    }
)


def _features_a_seam_resolves() -> set[str]:
    """Feature ids gate.py turns into a FeatureRequirement, read off its AST rather than its text.

    A text search would report `debuginfo`, whose name appears all over gate.py in
    `debuginfo_warning` and its reason codes - but that seam keys on a module-level BTF literal
    and evaluates no clause of the feature entry, so it supplies nothing and constrains nothing.
    """
    import ast
    import inspect

    from kdive.kernel_config import gate

    resolved: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(gate))):
        if not isinstance(node, ast.Call):
            continue
        # Match the callee however it is spelled - a bare name, or an attribute on an imported
        # module. Keying on `ast.Name` alone let `requirements.feature_requirement(X)` pass
        # unnoticed, and an unclassified call leaves the feature out of `resolved` entirely,
        # which reads here as "no seam evaluates it" and silences both halves of I2.
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called != "feature_requirement":
            continue
        args = [*node.args, *(kw.value for kw in node.keywords)]
        assert len(args) == 1, (
            f"feature_requirement call at gate.py line {node.lineno} takes {len(args)} arguments; "
            "this guard cannot tell which feature it resolves. Teach it that shape rather than "
            "letting the call go unclassified (#1860)."
        )
        arg = args[0]
        if isinstance(arg, ast.Constant):
            resolved.add(str(arg.value))
        elif isinstance(arg, ast.Name):
            resolved.add(str(getattr(gate, arg.id)))
        else:
            raise AssertionError(
                f"feature_requirement call at gate.py line {node.lineno} takes a "
                f"{type(arg).__name__} this guard cannot resolve to a feature id. Skipping it "
                "would drop the feature from the seam-evaluated set and silence I2 (#1860)."
            )
    return resolved


def test_the_seam_evaluated_feature_list_matches_the_features_the_gate_actually_resolves():
    # What keeps the hand-maintained map above from going stale: wiring a new feature into gate.py
    # fails here until its row - and its answers about the initrd and arch facts - are written
    # down.
    resolved = _features_a_seam_resolves()
    assert resolved  # non-vacuity: the AST walk must really find the calls
    assert resolved == set(_SEAM_SUPPLIES)


def _conditional_without_the_fact(
    features: tuple[FeatureRequirement, ...],
    seam_supplies: Mapping[str, SeamFacts],
    *,
    supplies: Callable[[SeamFacts], bool],
    declares: Callable[[Clause], bool],
) -> dict[str, list[str]]:
    """Features declaring a conditional clause a seam evaluates without supplying the condition.

    One walk for both axes of I2 (ADR-0544 §7), because the rule is the same one: ``declares``
    picks the clauses carrying the condition and ``supplies`` reads the seam's answer for that
    axis. A feature absent from ``seam_supplies`` is read by no seam at all, so its clause values
    are manifest metadata and constrain nothing - that is `serial_console`'s real position.
    """
    offenders: dict[str, list[str]] = {}
    for feature in features:
        facts = seam_supplies.get(feature.feature)
        if facts is None or supplies(facts):
            continue
        symbols = sorted(
            symbol
            for clauses in (feature.advertised, feature.gate_required)
            for clause in clauses
            if declares(clause)
            for symbol in clause.symbols
        )
        if symbols:
            offenders[feature.feature] = symbols
    return offenders


def _unless_initrd_without_the_fact(
    features: tuple[FeatureRequirement, ...], seam_supplies: Mapping[str, SeamFacts]
) -> dict[str, list[str]]:
    """Features carrying UNLESS_INITRD that a seam evaluates without supplying the initrd fact."""
    return _conditional_without_the_fact(
        features,
        seam_supplies,
        supplies=lambda facts: facts.initrd,
        declares=lambda clause: clause.built_in is BuiltIn.UNLESS_INITRD,
    )


def _guest_initramfs_without_the_fact(
    features: tuple[FeatureRequirement, ...], seam_supplies: Mapping[str, SeamFacts]
) -> dict[str, list[str]]:
    """Features carrying UNLESS_INITRD that a seam evaluates without supplying the boot model."""
    return _conditional_without_the_fact(
        features,
        seam_supplies,
        supplies=lambda facts: facts.guest_initramfs,
        declares=lambda clause: clause.built_in is BuiltIn.UNLESS_INITRD,
    )


def _arch_scoped_without_the_fact(
    features: tuple[FeatureRequirement, ...], seam_supplies: Mapping[str, SeamFacts]
) -> dict[str, list[str]]:
    """Features carrying an arch scope that a seam evaluates without supplying the arch."""
    return _conditional_without_the_fact(
        features,
        seam_supplies,
        supplies=lambda facts: facts.arch,
        declares=lambda clause: clause.arches is not None,
    )


def test_a_clause_is_unless_initrd_only_where_every_seam_reading_it_supplies_the_initrd_fact():
    # Invariant I2's UNLESS_INITRD half, landing with the field it guards (#1860). The rule: a
    # clause carries a conditional requirement only where every seam that evaluates its feature
    # supplies the condition. `gate_required` is NOT the boundary - rootfs_mount has an empty
    # refusal set and is still read live, through unmet_advertised_clauses.
    #
    # Without it, marking crash_capture UNLESS_INITRD would make its refusal quietly depend on a
    # fact crash_capture_refusal cannot supply: the strict default would fault every modular
    # kexec symbol on an initrd build that is in fact fine.
    #
    # THIS IS A REGRESSION GUARD, NOT A PROOF. It checks DECLARED field values against the
    # hand-maintained list above. A clause that is initrd-conditional *in fact* while carrying
    # NOT_REQUIRED passes it vacuously, and a seam wired up outside gate.py is invisible to it.
    assert _every_clause_symbol(FEATURE_REQUIREMENTS)  # non-vacuity: the roster is really walked
    assert _unless_initrd_without_the_fact(FEATURE_REQUIREMENTS, _SEAM_SUPPLIES) == {}
    # and the value is really in use on the live roster, so the empty result above is not the
    # answer to a question nothing asks
    assert _unless_initrd_without_the_fact(
        FEATURE_REQUIREMENTS,
        {ROOTFS_MOUNT: SeamFacts(initrd=False, guest_initramfs=True, arch=False)},
    ) == {ROOTFS_MOUNT: ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]}


def test_a_clause_is_unless_initrd_only_where_every_seam_supplies_the_boot_model_fact_too():
    # I2's UNLESS_INITRD half gained a second relief in ADR-0545: the condition is now "something
    # can load a module before root is mounted", answered by an uploaded initrd artifact OR by a
    # target lane whose guest builds its own initramfs. Both are seam-supplied facts with the same
    # strict default, so both carry the same obligation - a seam that evaluates an UNLESS_INITRD
    # clause while answering only one of them over-warns on the axis it cannot see. That is
    # exactly #1881: rootfs_mount_warning read the artifact and nothing about the boot model, and
    # every disk-image Run drew a deterministic false positive it had no way to silence.
    #
    # THIS IS A REGRESSION GUARD, NOT A PROOF, on the same terms as the initrd half above: it
    # checks DECLARED field values against the hand-maintained map, so a clause that is
    # boot-model-conditional in fact while carrying NOT_REQUIRED passes it vacuously.
    assert _every_clause_symbol(FEATURE_REQUIREMENTS)  # non-vacuity: the roster is really walked
    assert _guest_initramfs_without_the_fact(FEATURE_REQUIREMENTS, _SEAM_SUPPLIES) == {}
    # and the axis is really in use on the live roster: withdraw only the boot-model answer and
    # rootfs_mount is reported, which is what proves the empty result above is load-bearing
    assert _guest_initramfs_without_the_fact(
        FEATURE_REQUIREMENTS,
        {ROOTFS_MOUNT: SeamFacts(initrd=True, guest_initramfs=False, arch=False)},
    ) == {ROOTFS_MOUNT: ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]}


def test_a_clause_is_arch_scoped_only_where_every_seam_reading_it_supplies_the_arch():
    # Invariant I2's arch half, landing with the field it guards (ADR-0544 §7-§8, #1859). Same
    # rule as the initrd half above, second axis: rule 3's support checks skip a clause scoped to
    # an arch they were not given, so tagging a feature a seam reads without an arch would turn a
    # live check into silence rather than a fault - a warning that vanishes.
    #
    # The two seam-evaluated features are crash_capture and rootfs_mount. crash_capture's seams
    # resolve the Run's System profile arch as of #1875, so it may be tagged and is;
    # rootfs_mount's does not - complete_build runs against a Run and nothing on that path
    # resolves the System profile it will install to - so it may not. serial_console is read by
    # no seam and may.
    #
    # THIS IS A REGRESSION GUARD, NOT A PROOF, and the arch half is still the weaker of the two: a
    # clause that is arch-specific IN FACT while carrying arches=None passes it vacuously. What
    # changed with #1875 is that crash_capture is no longer an instance of that - the residual
    # ADR-0544 §7 recorded is closed, not still passing here silently.
    assert _every_clause_symbol(FEATURE_REQUIREMENTS)  # non-vacuity: the roster is really walked
    assert _arch_scoped_without_the_fact(FEATURE_REQUIREMENTS, _SEAM_SUPPLIES) == {}
    # and the field is really in use on the live roster, so the empty result above is not the
    # answer to a question nothing asks: withdraw crash_capture's arch answer and its two scoped
    # symbols are reported
    assert _arch_scoped_without_the_fact(
        FEATURE_REQUIREMENTS,
        {CRASH_CAPTURE: SeamFacts(initrd=False, guest_initramfs=False, arch=False)},
    ) == {CRASH_CAPTURE: ["FW_CFG_SYSFS", "FW_CFG_SYSFS", "RANDOMIZE_BASE"]}
    # and a feature no seam reads is still allowed to carry one, which is serial_console's place
    assert _arch_scoped_without_the_fact(
        FEATURE_REQUIREMENTS,
        {"serial_console": SeamFacts(initrd=True, guest_initramfs=True, arch=False)},
    ) == {"serial_console": ["HVC_CONSOLE", "SERIAL_8250", "SERIAL_8250_CONSOLE"]}


def test_the_three_invariants_report_a_seam_evaluated_feature_and_spare_the_other_two_cases():
    # The three dispositions each check has to tell apart, on one synthetic feature so the arms
    # differ only in what the seam supplies. All three axes on the same fixture, because they are
    # one invariant and a fixture that exercised only some would let the others' wiring rot.
    smuggled = FeatureRequirement(
        "not_a_real_feature",
        "synthetic fixture for the invariants above",
        advertised=(
            Clause(frozenset({"VIRTIO_PCI"}), BuiltIn.UNLESS_INITRD, arches=frozenset({"x86_64"})),
        ),
        gate_required=(
            Clause(frozenset({"VIRTIO_BLK"}), BuiltIn.UNLESS_INITRD, arches=frozenset({"x86_64"})),
        ),
        enforcement=Enforcement.UPLOAD_REFUSAL,
    )
    both = ["VIRTIO_BLK", "VIRTIO_PCI"]
    all_supplied = SeamFacts(initrd=True, guest_initramfs=True, arch=True)
    # Each axis paired with the facts that withhold exactly that one, so a check wired to read a
    # neighbour's field is reported here rather than passing on the neighbour's answer.
    axes = (
        (_unless_initrd_without_the_fact, all_supplied._replace(initrd=False)),
        (_guest_initramfs_without_the_fact, all_supplied._replace(guest_initramfs=False)),
        (_arch_scoped_without_the_fact, all_supplied._replace(arch=False)),
    )
    supplies_neither = {
        "not_a_real_feature": SeamFacts(initrd=False, guest_initramfs=False, arch=False)
    }
    for check, withholding in axes:
        # evaluated by a seam that supplies nothing -> reported by every check, from both fields
        assert check((smuggled,), supplies_neither) == {"not_a_real_feature": both}, check
        # evaluated by a seam that does supply the fact -> allowed
        assert check((smuggled,), {"not_a_real_feature": all_supplied}) == {}, check
        # read by no seam at all -> allowed, and this is serial_console's position today
        assert check((smuggled,), {}) == {}, check
        # the axes are independent: withholding this one reports here and nowhere else
        supplied = {"not_a_real_feature": withholding}
        assert check((smuggled,), supplied) == {"not_a_real_feature": both}, check
        for other, _ in axes:
            if other is not check:
                assert other((smuggled,), supplied) == {}, (check, other)
    assert "serial_console" not in _SEAM_SUPPLIES
