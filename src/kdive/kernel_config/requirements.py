"""Feature -> required CONFIG_* registry (ADR-0318, ADR-0544, ADR-0546).

Single source of truth for both the advertised manifest and the arming gate. Each feature
carries an ``advertised`` superset (guidance shown to the agent) and a deliberately narrower
``gate_required`` subset (what the gate refuses on). Each clause is an OR-group: satisfied
when any member symbol is enabled. Symbol names are bare (no ``CONFIG_`` prefix), matching
:func:`kdive.kernel_config.parse.parse_kernel_config`.

Beside those two clause tuples each feature states its :class:`Enforcement` - where an omission
surfaces (ADR-0546). That is the one fact about *kdive* the manifest carries; everything else in
an entry is a fact about the kernel.

One rule bounds what a clause may name (ADR-0544 §1, #1854): a symbol that is unsettable **in
principle** - prompt-less, or existing only because something else ``select``s it - is never a
clause member, because ``make olddefconfig`` discards it out of a config fragment and naming it
sends the agent after the one thing it cannot do. The clause naming its settable selector stands
in its place. A symbol that is settable once a prerequisite holds stays a clause member, and the
prerequisite becomes its own clause AND-ed alongside it.

Beyond the symbols, a clause carries two per-clause axes and no more: a built-in requirement
(ADR-0544 §2) and an arch scope (§3). Anything else expressible as an AND-ed prerequisite clause
stays a clause rather than becoming a fourth field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from kdive.serialization import JsonValue


class BuiltIn(StrEnum):
    """Whether a clause is satisfied by a module, and if not, under what condition (§2, #1860).

    Three values rather than a bool, because the two reasons a symbol must be ``=y`` are not the
    same reason and do not relax under the same condition: an initramfs gives the boot ordering
    something to load a module from, and does nothing at all for a Kconfig dependency written
    ``depends on FOO=y``.
    """

    NOT_REQUIRED = "not_required"
    """``=m`` satisfies the clause - KASAN, ftrace, kcov, the BPF symbols. The default."""

    REQUIRED = "required"
    """``=y`` always: a module form does not deliver the feature at any point in the boot."""

    UNLESS_INITRD = "unless_initrd"
    """``=y`` unless something can load the module before root is mounted (ADR-0545).

    Two facts answer that and either alone relieves the clause: the build uploaded an initrd
    artifact, or the target boots through its own bootloader and builds its initramfs in-guest.
    """


class Enforcement(StrEnum):
    """Where an omission of this feature's symbols surfaces (ADR-0546, #1867).

    Replaces the bare ``gated`` bool the manifest used to ship. That bool answered "does kdive
    refuse?", which an agent read as "must I build this?" - and the two differ on ``sysrq``, whose
    refusal is real but arrives from the feature's own handler rather than from the upload lane.

    Each value is defined for the agent in :data:`ENFORCEMENT_LEGEND`, which the served contract
    document carries beside the entries, so the flag can no longer reach a reader without its
    meaning.
    """

    UPLOAD_REFUSAL = "upload_refusal"
    """kdive reads the uploaded config and refuses the arming action - ``crash_capture``."""

    UPLOAD_ADVISORY = "upload_advisory"
    """kdive reads the uploaded config and warns; the action succeeds - ``rootfs_mount``."""

    RUNTIME_REFUSAL = "runtime_refusal"
    """No config check at all; the feature's own handler refuses on a booted kernel - ``sysrq``."""

    UNCHECKED = "unchecked"
    """No kdive check reads this entry's requirements. The default, and most of the registry."""


# The served legend. Sentences, not labels: the defect ADR-0546 closes is a flag with no
# definition, so the definition ships in the same payload as the value. Keyed by the enum so a
# value cannot exist without a definition or a definition without a value (asserted in
# tests/kernel_config/test_requirements.py). Agent-facing prose - no record references here.
ENFORCEMENT_LEGEND: Final[dict[Enforcement, str]] = {
    Enforcement.UPLOAD_REFUSAL: (
        "kdive reads the effective_config you uploaded with the build and refuses the action "
        "that arms this feature, with a configuration error naming the symbols you are missing. "
        "The entry's refuses_on lists exactly the clauses it refuses on; every other clause in "
        "requirements is advice and can never produce a refusal."
    ),
    Enforcement.UPLOAD_ADVISORY: (
        "kdive reads the effective_config you uploaded with the build and returns a warning in "
        "the response data. The action still succeeds, so a symbol missing here costs you a "
        "notice rather than a refusal."
    ),
    Enforcement.RUNTIME_REFUSAL: (
        "kdive does not read your config for this feature, so a clean upload tells you nothing "
        "about it. The refusal arrives when you invoke the feature, on a kernel already built, "
        "installed and booted, and recovering costs another build, install and boot. This is a "
        "decision to make before you build, not one you can revisit afterwards."
    ),
    Enforcement.UNCHECKED: (
        "No kdive check reads this entry's requirements at any point. Omitting it costs you the "
        "feature itself and nothing else kdive can observe; the summary says what that is."
    ),
}


@dataclass(frozen=True, slots=True)
class Clause:
    """One OR-group of kernel symbols: satisfied when any member is enabled.

    A record rather than a bare ``frozenset`` so that the per-clause axes arrive as fields beside
    ``symbols`` rather than as incompatible extensions to one type. Both ``built_in`` and
    ``arches`` are per clause and not per symbol because no clause in the registry mixes symbols
    that differ on either (ADR-0544).

    ``UNLESS_INITRD`` states a condition; the *answers* are facts about the build and the target,
    so the seam supplies them (``has_initrd``, ``guest_builds_initramfs``). ``arches`` is the same
    shape: the clause names the arches it applies to and the seam supplies which one is in play
    (``arch``). ``None`` means every arch. A clause may only carry a conditional value where every
    seam that evaluates its feature passes **every** fact that value needs - see the invariant in
    ``tests/kernel_config/test_requirements.py``.
    """

    symbols: frozenset[str]
    built_in: BuiltIn = BuiltIn.NOT_REQUIRED
    arches: frozenset[str] | None = None


# Arch scopes for clause tags. Spelled here rather than imported from domain.platform: this
# registry parses a `.config` and answers questions about symbols, and making a static data table
# depend on the provisioning layer to name two strings would invert that. A test asserts every
# value stays a subset of SUPPORTED_ARCHES (ADR-0544 §3).
_X86_ONLY: Final = frozenset({"x86_64"})
_PPC64LE_ONLY: Final = frozenset({"ppc64le"})

CRASH_CAPTURE = "crash_capture"
CRASH_CAPTURE_RHEL_GUEST = "crash_capture_rhel_guest"
SYSRQ = "sysrq"
ROOTFS_MOUNT = "rootfs_mount"


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    """One debug/platform feature and the kernel symbols it wants.

    ``advertised`` is the full recommended set (manifest guidance); ``gate_required`` is the
    minimal subset the gate refuses on (``()`` = advertise-only, never gated). Both are ordered
    tuples of OR-group clauses.

    ``enforcement`` is authored rather than derived, because the distinction that matters is not
    derivable: ``sysrq`` and ``kasan`` both carry ``gate_required=()`` and are not the same kind of
    optional (ADR-0546). Its one derivable half *is* checked, at construction - a refusal set and
    ``UPLOAD_REFUSAL`` imply each other, and #1861 is what a registry that let them drift ships.
    """

    feature: str
    summary: str
    advertised: tuple[Clause, ...]
    gate_required: tuple[Clause, ...] = ()
    enforcement: Enforcement = Enforcement.UNCHECKED

    def __post_init__(self) -> None:
        refuses = self.enforcement is Enforcement.UPLOAD_REFUSAL
        if bool(self.gate_required) is not refuses:
            raise ValueError(
                f"{self.feature}: enforcement={self.enforcement.value} disagrees with a "
                f"{'non-empty' if self.gate_required else 'empty'} gate_required - a refusal set "
                "and upload_refusal imply each other"
            )


def _plain(*symbols: str) -> tuple[Clause, ...]:
    return tuple(Clause(frozenset({s})) for s in symbols)


# lib/Kconfig.debug:262-323 is the "Debug information" `choice`. Three of its four members produce
# real DWARF and select DEBUG_INFO - DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT (:281, selects at :283),
# DEBUG_INFO_DWARF4 (:293, :295) and DEBUG_INFO_DWARF5 (:305, :307); the fourth, DEBUG_INFO_NONE
# (:275), is the off setting. Named once because two features need the same three symbols for
# different reasons (#1855): `debuginfo` advertises them as the feature itself, and `bpf_tracing`
# AND-s them in as the prerequisite DEBUG_INFO_BTF is only settable behind. Two copies would let a
# later choice member reach one entry and not the other.
_DWARF_CHOICE_MEMBERS: Final[frozenset[str]] = frozenset(
    {"DEBUG_INFO_DWARF5", "DEBUG_INFO_DWARF4", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT"}
)


FEATURE_REQUIREMENTS: tuple[FeatureRequirement, ...] = (
    FeatureRequirement(
        ROOTFS_MOUNT,
        "Mount the root filesystem the guest boots from. Local-libvirt direct-kernel-boots the "
        "kernel you install, with a whole-disk ext4 qcow2 as root (root=/dev/vda, and no "
        "initramfs unless your build uploads an initrd artifact or the kernel embeds its own "
        "initramfs); a remote or agent-uploaded rootfs is commonly XFS (RHEL-family base "
        "images). kdive does not know which family your guest uses, so it asks only that the "
        "kernel can mount at least one of them plus the virtio-blk root device - build in the "
        "one your rootfs actually uses. kdive's upload-time advisory recognizes an uploaded "
        "artifact and a disk-image target, whose guest builds its own initramfs; it cannot see "
        "an embedded initramfs, so a modular config may still draw a harmless warning on a "
        "direct-kernel kernel that in fact boots fine.",
        # Both clauses are UNLESS_INITRD (#1860). The direct-kernel boot mounts root before any
        # module can be loaded, so EXT4_FS=m / XFS_FS=m / VIRTIO_BLK=m are a kernel that panics on
        # an unmountable root - unless something can load a module first. Two facts answer that
        # and `rootfs_mount_warning` supplies both (ADR-0545): the build uploaded an initrd
        # artifact (has_initrd), or the target boots through its own bootloader and runs dracut
        # in-guest (guest_builds_initramfs, #1881). Neither sees an embedded initramfs, so this
        # clause is still not relieved for such a kernel (#1876) - which is why the summary above
        # carries a caveat that this clause's advisory does not.
        (
            Clause(frozenset({"EXT4_FS", "XFS_FS"}), BuiltIn.UNLESS_INITRD),
            Clause(frozenset({"VIRTIO_BLK"}), BuiltIn.UNLESS_INITRD),
        ),
        # Advisory, not gated: `rootfs_mount_warning` reads these advertised clauses at
        # runs.complete_build and returns `kernel_missing_boot_config` in the success data, so the
        # completion succeeds either way (ADR-0330). `gate_required` stays empty.
        enforcement=Enforcement.UPLOAD_ADVISORY,
    ),
    FeatureRequirement(
        CRASH_CAPTURE,
        "Reserve a crashkernel and capture a vmcore via kdump. Guest-family-independent only: "
        "these symbols get the capture kernel loaded, not the vmcore written. A RHEL-family guest "
        "needs the crash_capture_rhel_guest set as well.",
        # KEXEC_CORE and VMCORE_INFO appear in neither field: both are bare prompt-less bools
        # (kernel/Kconfig.kexec:11 and :8) no fragment can set - olddefconfig drops the line, so
        # naming one sends the agent after the one thing it cannot do. Nor do they carry signal
        # on a modern kernel: KEXEC (:20) and KEXEC_FILE (:38) select KEXEC_CORE, and CRASH_DUMP
        # (:97) selects VMCORE_INFO (so does PROC_KCORE, fs/proc/Kconfig:32), and every one of
        # those selectors is a clause of the refusal set below - so a coherent .config carrying a
        # selector carries the symbol it selects.
        #
        # Where they did carry signal, the signal was wrong. VMCORE_INFO does not exist before
        # Linux 6.9 - it was split out of CRASH_CORE - so a complete, working pre-6.9 config
        # could not satisfy that clause at any setting, and the gate refused crash capture over a
        # symbol whose name is absent from that kernel's Kconfig. Past that, the only config left
        # to refuse on is an internally inconsistent one (truncated or hand-edited). Both are the
        # false refusal ADR-0318's fail-open boundary exists to avoid.
        #
        # Two symbols are x86-only across the arches kdive provisions, verified against upstream
        # Linux v7.0 (3131ff5a117498bb4b9db3a238bb311cbf8383ce) - #1875:
        #
        #   FW_CFG_SYSFS (drivers/firmware/Kconfig:122) `depends on SYSFS && (ARM || ARM64 ||
        #   PARISC || PPC_PMAC || RISCV || SPARC || X86)`. PPC_PMAC is the only powerpc arm, and
        #   PPC_PMAC (arch/powerpc/platforms/powermac/Kconfig:2) itself `depends on PPC_BOOK3S &&
        #   CPU_BIG_ENDIAN` - so no *little-endian* powerpc kernel can set it at any machine type,
        #   which is stronger than "not on the pseries kdive boots" and holds for all of ppc64le.
        #   That PPC_PMAC line is unchanged back to v4.18, the oldest kernel in the rootfs catalog
        #   (rocky-kdive-ready-8), so this relief does not drift across the supported range - which
        #   matters more than usual because it is a relief: were it wrong, a ppc64le kernel that
        #   could set the symbol would be armed and capture nothing.
        #
        #   Note this arm is NOT x86-specific in the kernel - ARM64, RISCV and SPARC all offer the
        #   symbol. `_X86_ONLY` is correct only because kdive provisions exactly {x86_64, ppc64le};
        #   a third arch must re-read this, which the guard in tests/kernel_config/ forces.
        #
        #   RANDOMIZE_BASE exists under arch/powerpc (:688) but `depends on PPC_85xx && FLATMEM`,
        #   a 32-bit e500 platform, so it is likewise unreachable on ppc64le. Advertised only, so
        #   it can never refuse - the tag keeps the manifest from advising a symbol no ppc64le
        #   agent can act on, which is the same defect one field over.
        #
        # RELOCATABLE stays unscoped: arch/powerpc/Kconfig:665 offers it under `depends on PPC64
        # || (FLATMEM && (44x || PPC_85xx))`, and PPC64 holds for ppc64le, so it is a real prompt
        # on both arches. Line numbers move between releases (ADR-0544); the symbol and its
        # dependency expression are the claim, the numbers are pointers.
        _plain("KEXEC", "KEXEC_FILE", "CRASH_DUMP", "PROC_VMCORE")
        + (
            Clause(frozenset({"FW_CFG_SYSFS"}), arches=_X86_ONLY),
            Clause(frozenset({"RELOCATABLE"})),
            Clause(frozenset({"RANDOMIZE_BASE"}), arches=_X86_ONLY),
        ),
        gate_required=(
            Clause(frozenset({"KEXEC", "KEXEC_FILE"})),  # either load syscall suffices
            Clause(frozenset({"CRASH_DUMP"})),
            Clause(frozenset({"PROC_VMCORE"})),
            Clause(frozenset({"FW_CFG_SYSFS"}), arches=_X86_ONLY),
            Clause(frozenset({"RELOCATABLE"})),
        ),
        # The only refusing entry: both crash seams turn `unmet_clauses` into a
        # CONFIGURATION_ERROR. The manifest publishes the five clauses above as `refuses_on`, so
        # an agent can see that RANDOMIZE_BASE - advertised, never gated - cannot refuse (#1867).
        enforcement=Enforcement.UPLOAD_REFUSAL,
    ),
    FeatureRequirement(
        CRASH_CAPTURE_RHEL_GUEST,
        "Extra symbols a RHEL-family guest (RHEL/Rocky/AlmaLinux/CentOS Stream/Fedora) needs "
        "before kdump can actually write a vmcore. All are filesystem- or initramfs-dependent, so "
        "they are advisory, never gated: kdive cannot tell which OS your guest runs. XFS_FS - the "
        "RHEL root filesystem the capture kernel must mount to write the core (a stock defconfig "
        "builds EXT4 but not XFS). SQUASHFS/SQUASHFS_ZSTD/EROFS_FS/OVERLAY_FS/BLK_DEV_LOOP - "
        "dracut builds the kdump initramfs as a compressed squashfs or erofs image over a loop "
        "device with an overlay; which one varies by release, so build all of them in. "
        "KEXEC_FILE - RHEL's kdump service loads the capture kernel with kexec_file_load, not the "
        "legacy kexec_load, so a KEXEC-only kernel arms crashkernel= and then captures nothing. "
        "Skip this feature entirely for a non-RHEL guest whose root and initramfs differ.",
        _plain(
            "XFS_FS",
            "SQUASHFS",
            "SQUASHFS_ZSTD",
            "EROFS_FS",
            "OVERLAY_FS",
            "BLK_DEV_LOOP",
            "KEXEC_FILE",
        ),
    ),
    FeatureRequirement(
        "ikconfig",
        "Recover the .config a running guest kernel was built from, by reading /proc/config.gz "
        "inside the guest. The reason to want it: olddefconfig silently drops any symbol whose "
        "dependencies are unmet, so the config you wrote and the config you got can differ, and "
        "this readback settles that from inside a booted guest without depending on the installer "
        "having left a /boot/config-<version> behind. Skip it when you kept the .config and "
        "uploaded it as the build's effective_config - kdive checks that uploaded copy, never "
        "this one, so the readback serves you and your in-guest tools rather than kdive's own "
        "advisories. The cost is close to nothing: a gzipped copy of the .config in the kernel's "
        "read-only data (tens of kilobytes) and no runtime cost at all. Build IKCONFIG in rather "
        "than as a module, or /proc/config.gz appears only while that module is loaded; "
        "IKCONFIG_PROC is what creates the file.",
        # init/Kconfig:767 IKCONFIG is a tristate (:768) whose data is .incbin-ed as
        # kernel/config_data.gz into .rodata (kernel/configs.c:23-32); :779 IKCONFIG_PROC is the
        # half that creates /proc/config.gz, "depends on IKCONFIG && PROC_FS" at :781.
        #
        # IKCONFIG is REQUIRED, not UNLESS_INITRD (#1860): /proc/config.gz exists only while the
        # module is loaded, so =m does not deliver the readback at all and no initrd relieves it -
        # the same defect class as the boot clauses, on the feature whose own summary already says
        # to build it in. IKCONFIG_PROC is a bool with no module form, so it needs no value.
        (
            Clause(frozenset({"IKCONFIG"}), BuiltIn.REQUIRED),
            Clause(frozenset({"IKCONFIG_PROC"})),
        ),
    ),
    FeatureRequirement(
        "debuginfo",
        "Enable only for live drgn/gdb symbol resolution or offline vmcore analysis. Embeds "
        "DWARF tables in every .ko - can grow the module tree 10-50x and slow upload and "
        "install. Omit for boot-time crash reproducers and console-log investigations where no "
        "post-boot introspection is needed. What is below is the DWARF half, which is what gdb "
        "and an offline vmcore read. In-guest drgn-live reads BTF from /sys/kernel/btf instead - "
        "the DWARF vmlinux is not on the guest rootfs - so if you are going to run drgn inside "
        "the guest, add DEBUG_INFO_BTF from the bpf_tracing set on top of a DWARF member here, or "
        "upload a matching vmlinux; DWARF alone leaves that session unable to resolve a symbol.",
        # The clause is exactly the DWARF `choice` members, and each of the two edits that made it
        # so is load-bearing (#1855).
        #
        # DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT belongs because it is a choice member like the other
        # two and yields real DWARF. Omitting it faulted a kernel built on the toolchain default
        # for all three symbols at once, which is the one direction of error this advisory cannot
        # afford: it tells a complete debuginfo build to spend a rebuild.
        #
        # DEBUG_INFO_BTF does not belong. It is not a choice member (lib/Kconfig.debug:398, inside
        # `if DEBUG_INFO` at :325-455) and selects nothing, so offering it as a third alternative
        # named a path a bare config cannot take - olddefconfig drops the line. It is also not this
        # feature: an offline vmcore and gdb want DWARF, and BTF keeps its home under bpf_tracing,
        # where the same prerequisite is now stated as its own clause.
        #
        # DEBUG_INFO itself stays unadvertised: :249 is a bare prompt-less bool no fragment can
        # set, and it carries no signal here either, because all three members below select it -
        # DEBUG_INFO=n forces every one of them off and this clause reports the same kernel.
        (
            Clause(_DWARF_CHOICE_MEMBERS),
            Clause(frozenset({"DEBUG_KERNEL"})),
        ),
    ),
    FeatureRequirement(
        SYSRQ,
        "Trigger magic SysRq diagnostics in the guest from the host - task states (t), blocked "
        "tasks (w), memory (m), locks (d), registers (p), per-CPU backtraces (l), timers (q) - "
        "which is how you get state out of a guest that has wedged and no longer answers over "
        "SSH. Nothing checks this at upload time, so it is a decision to make now: MAGIC_SYSRQ is "
        "a plain bool with no module form that cannot be turned on after the build, and the "
        "refusal lands at the far end - diagnostic_sysrq fails with a configuration error naming "
        "the symbol rather than returning an empty capture, and recovering costs a rebuild, a "
        "reinstall and a reboot. kdive's config checks cover crash capture, the root filesystem "
        "and debuginfo only, so a clean build upload tells you nothing about SysRq. kdive injects "
        "the Alt+SysRq chord through the guest's input layer, so an x86 guest also needs a PS/2 "
        "keyboard driver (i8042/atkbd) for the keystroke to arrive, and the guest's kernel.sysrq "
        "sysctl decides separately which of these commands it permits once the kernel carries the "
        "feature at all. Cost is the SysRq handler code in kernel text and nothing at runtime "
        "until a key is sent, so skip it only if you are certain you will never need to question "
        "a wedged guest.",
        # lib/Kconfig.debug:665 MAGIC_SYSRQ is a bool (:666), "depends on !UML" (:667) - no module
        # form, so it is settable only at build time; :679 MAGIC_SYSRQ_DEFAULT_ENABLE and the
        # guest's kernel.sysrq sysctl are the separate runtime mask, which is why a MAGIC_SYSRQ=y
        # kernel can still refuse an individual command. Advertise-only per ADR-0318: sysrq is
        # enforced by the runtime detection in the diagnostic_sysrq handler, so a refusal set
        # here would be read by nothing while shipping gated: true in the manifest (#1861).
        #
        # RUNTIME_REFUSAL is the value #1867 exists for. With a bare `gated` bool this entry was
        # indistinguishable from kasan and the six other cheap-to-omit entries, which is the
        # opposite of the truth: MAGIC_SYSRQ has no module form, so the refusal at diagnostic_sysrq
        # costs another build, install and boot. Empty refusal set, real refusal.
        _plain("MAGIC_SYSRQ"),
        enforcement=Enforcement.RUNTIME_REFUSAL,
    ),
    FeatureRequirement(
        "kasan",
        "Enable to catch slab and stack out-of-bounds accesses, use-after-free and double-free "
        "at the instruction that commits them rather than at the later corruption. Generic mode "
        "spends about 1/8 of memory on shadow tables, adds roughly 50% to every allocation and "
        "runs about 3x slower, so give a KASAN guest more RAM than the same workload needs "
        "without it. The modes are alternatives, not additions: pick exactly one of "
        "KASAN_GENERIC, or KASAN_SW_TAGS/KASAN_HW_TAGS on arm64. Generic and software tag-based "
        "additionally take one instrumentation form - KASAN_INLINE (about an x2 speedup on some "
        "workloads, at a much larger kernel .text) or KASAN_OUTLINE (smaller and slower); "
        "hardware tag-based has no such choice, so do not set either symbol with it. STACKTRACE "
        "turns a report into allocation and free backtraces. Omit for console-log-only "
        "reproducers, where the report adds nothing. Pair with debuginfo only when you will run "
        "drgn or gdb on the result - KASAN's text growth plus DWARF in every .ko makes the "
        "upload tarball much larger. Do not combine with kcsan: the kernel builds KCSAN only "
        "when KASAN is off, so a config with both gives you KASAN and silently drops KCSAN.",
        (
            Clause(frozenset({"KASAN"})),
            Clause(frozenset({"KASAN_GENERIC", "KASAN_SW_TAGS", "KASAN_HW_TAGS"})),
            Clause(frozenset({"STACKTRACE"})),
        ),
    ),
    FeatureRequirement(
        "kcsan",
        "Kernel Concurrency Sanitizer: finds a data race - two tasks touching the same memory "
        "with at least one write and no lock or atomic ordering between them - before it "
        "corrupts anything. It instruments every memory access and then stalls the accessing "
        "task for tens of microseconds watching for a racing access, so the kernel is slow and "
        "its timing is not representative: use it to find a race, not to reproduce a "
        "timing-sensitive one. Needs DEBUG_KERNEL. Cannot be combined with kasan - the kernel "
        "builds KCSAN only when KASAN is off. Composes with debuginfo; the reports name "
        "functions either way.",
        _plain("DEBUG_KERNEL", "KCSAN"),
    ),
    FeatureRequirement(
        "kfence",
        "Kernel Electric-Fence: catches slab out-of-bounds accesses (reads and writes), "
        "use-after-free and "
        "invalid-free on a sampled fraction of allocations by putting them between guard pages. "
        "Cost is bounded by the sample interval (KFENCE_SAMPLE_INTERVAL, milliseconds between "
        "sampled allocations, default 100) and the pool size (KFENCE_NUM_OBJECTS, two pages "
        "each, default 255), so it is cheap enough to leave on for a long soak - but it only "
        "sees a bug that lands on a sampled object, which is why it is the wrong tool for a "
        "reproducer you can trigger on demand. Use kasan for that. Composes with debuginfo.",
        _plain("KFENCE"),
    ),
    FeatureRequirement(
        "kmemleak",
        "Kernel memory leak detector: reports kmalloc/vmalloc allocations no longer reachable "
        "from any pointer, which is the class of bug behind a slow-growth out-of-memory. It "
        "records a stack trace for every allocation and periodically scans all of kernel "
        "memory, so throughput and memory both suffer - a soak kernel, not a reproduction "
        "kernel. Needs DEBUG_KERNEL, and reports through /sys/kernel/debug/kmemleak, so debugfs "
        "must be mounted in the guest. Composes with debuginfo.",
        _plain("DEBUG_KERNEL", "DEBUG_KMEMLEAK"),
    ),
    FeatureRequirement(
        "lockdep",
        "Lock-correctness validator: reports lock-ordering inversions (the ABBA pattern behind a "
        "deadlock) and irq-unsafe locking the first time the kernel takes the path, without the "
        "deadlock having to happen. PROVE_LOCKING pulls in LOCKDEP and the "
        "DEBUG_SPINLOCK/DEBUG_MUTEXES/DEBUG_LOCK_ALLOC set, so those need no separate entry; it "
        "adds bookkeeping to every lock operation and a fixed static table sized by the "
        "LOCKDEP_*_BITS knobs, and it reports only the first violation before switching itself "
        "off. DEBUG_ATOMIC_SLEEP is separate and catches sleeping in atomic context. Needs "
        "DEBUG_KERNEL. Add LOCK_STAT for contention counts at further cost. Composes with "
        "debuginfo.",
        _plain("DEBUG_KERNEL", "PROVE_LOCKING", "DEBUG_ATOMIC_SLEEP"),
    ),
    FeatureRequirement(
        "ftrace",
        "Function and event tracing through tracefs (/sys/kernel/tracing): function and "
        "function-graph tracers, static tracepoints, and kprobe/uprobe dynamic events. This "
        "answers which code path the kernel took before it failed; it finds no memory "
        "corruption. Two symbols matter but are not listed below because the kernel turns them "
        "on for you and neither has a Kconfig prompt: TRACING is what builds tracefs, and "
        "DYNAMIC_FTRACE patches every instrumented call site to a nop until a tracer is enabled. "
        "That nop patching is why an idle ftrace kernel costs kernel text and close to nothing "
        "at runtime, and why the cost arrives with the events you actually record. Add "
        "UPROBE_EVENTS to probe userspace as well. Composes with debuginfo; BTF from the "
        "bpf_tracing set additionally gives typed probe arguments.",
        _plain("FTRACE", "FUNCTION_TRACER", "KPROBES", "KPROBE_EVENTS"),
    ),
    FeatureRequirement(
        "bpf_tracing",
        "Prerequisites for attaching BPF programs to kprobes, tracepoints and perf events - what "
        "bpftrace and BCC need in the guest. BPF_EVENTS is the symbol that permits the attach, "
        "and the kernel turns it on only when BPF_SYSCALL, PERF_EVENTS and a probe-event source "
        "(KPROBE_EVENTS or UPROBE_EVENTS) are all present, so build the whole set or the attach "
        "fails with nothing to point at - BPF_EVENTS itself has no Kconfig prompt and cannot be "
        "set from a fragment, so it is not listed below. Either probe-event source satisfies it, "
        "but they are not equivalent at attach time: KPROBE_EVENTS additionally needs KPROBES "
        "(see the ftrace set, and note a stock arm64 defconfig omits it), while UPROBE_EVENTS "
        "gives you userspace probes only. DEBUG_INFO_BTF is what lets a tool "
        "resolve struct layouts without kernel headers; pahole 1.22+ derives it from DWARF, so "
        "it needs a full "
        "debuginfo build (not reduced, not split) and lengthens the build. BPF_JIT is optional: "
        "without it programs still attach and run, under the interpreter. Runtime cost is close "
        "to nothing until a program is attached, then it is whatever that program does.",
        # The DWARF clause is BTF's prerequisite, not a second feature (#1855). DEBUG_INFO_BTF
        # (lib/Kconfig.debug:398) is a real prompt, so it stays the symbol this entry names - but
        # it sits inside `if DEBUG_INFO` (:325-455) and selects nothing, so a fragment that sets it
        # over a bare config is dropped by olddefconfig and the agent gets a kernel with no BTF and
        # no error. Stating the prerequisite as its own AND-ed OR-group is what makes the advertised
        # set say "pick a DWARF member, then BTF" in the order the two have to be done.
        (
            Clause(frozenset({"BPF_SYSCALL"})),
            Clause(frozenset({"PERF_EVENTS"})),
            Clause(frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"})),
            Clause(_DWARF_CHOICE_MEMBERS),
            Clause(frozenset({"DEBUG_INFO_BTF"})),
        ),
    ),
    FeatureRequirement(
        "fault_injection",
        "Make chosen kernel allocations and I/O submissions fail on demand, to reach the error "
        "path a stress test never does - the class of bug that only shows up when kmalloc "
        "returns NULL. FAULT_INJECTION alone builds the framework and injects nothing: pick at "
        "least one site (FAILSLAB, FAIL_PAGE_ALLOC, FAIL_MAKE_REQUEST, FAIL_IO_TIMEOUT, "
        "FAIL_FUTEX). All five register their knobs only under FAULT_INJECTION_DEBUG_FS, which "
        "in turn needs DEBUG_FS and SYSFS, so debugfs is the interface - FAULT_INJECTION_CONFIGFS "
        "is a separate path only a driver that opted into it exposes, and it drives none of the "
        "five. Needs DEBUG_KERNEL. Runtime cost is a probability check at the instrumented call "
        "sites and is close to nothing while the configured failure rate is zero, so this suits "
        "a long-running guest. Composes with debuginfo.",
        (
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
    ),
    FeatureRequirement(
        "kcov",
        "Per-task code-coverage feedback through /sys/kernel/debug/kcov, which is what a "
        "coverage-guided fuzzer such as syzkaller steers on. It finds no bug by itself; it tells "
        "the fuzzer which inputs reached new code. Two knobs are yours to choose and so are not "
        "listed below. KCOV_INSTRUMENT_ALL (default y) is the expensive one - it compiles a "
        "coverage callback into every kernel function, which is what a whole-kernel fuzzing run "
        "wants and is why such a kernel is slow; set it to n and mark only the subsystem under "
        "test to keep the cost local. KCOV_ENABLE_COMPARISONS additionally records comparison "
        "operands, which gets a fuzzer past magic-value checks at further cost. KCOV builds "
        "debugfs in for you. Composes with debuginfo.",
        _plain("KCOV"),
    ),
    FeatureRequirement(
        "serial_console",
        "The serial console is the only channel kdive has for kernel output from a guest with no "
        "working SSH: boot progress, the readiness marker, oops and panic output, and every SysRq "
        "capture arrive on it. Build the one your arch uses - kdive boots its libvirt domains "
        "with console=ttyS0 on x86, which SERIAL_8250_CONSOLE drives, while a ppc64le pseries "
        "guest consoles on hvc0 and needs HVC_CONSOLE instead, where SERIAL_8250_CONSOLE does "
        "nothing. VIRTIO_PCI is the transport half and the boot-fatal one: the root disk and NIC "
        "are PCI virtio devices, so without it the VIRTIO_BLK driver from the rootfs_mount set "
        "never binds and the guest panics on an unmountable root before any console setting "
        "matters. Build it in (=y) rather than as a module: unless your build uploads an initrd "
        "artifact or the kernel embeds its own initramfs, the direct-kernel boot has no "
        "initramfs to load that module from before root is mounted (a guest that boots through "
        "its own bootloader and builds an initramfs with dracut is not affected either way). "
        "Nothing below is checked on your behalf - kdive's "
        "config checks cover crash capture, the root filesystem and debuginfo, so no symbol "
        "below draws a warning at any value, and this summary is the whole of the "
        "notice you get. There is no reason to skip the console for your arch or the transport, "
        "and nothing to weigh against them: both cost kernel text and no measurable runtime. "
        "SERIAL_8250_CONSOLE additionally needs SERIAL_8250 itself built in - it is not offered "
        "against a modular 8250.",
        # drivers/tty/serial/8250/Kconfig:70 SERIAL_8250_CONSOLE is a bool whose :72 "depends on
        # SERIAL_8250=y" is why the modular 8250 note is a real constraint; drivers/tty/hvc/
        # Kconfig:14 HVC_CONSOLE is the pseries console, "depends on PPC_PSERIES" at :16.
        # drivers/virtio/Kconfig:50-62 VIRTIO_PCI is a *tristate* (:51), "depends on PCI" (:52),
        # whose own help says "If unsure, say M" (:61) - wrong for a direct-kernel boot with no
        # initramfs. It is the transport the rootfs_mount VIRTIO_BLK disk binds through on every
        # PCI machine type kdive boots (q35 and pseries here, i440fx on a remote-libvirt host).
        # No seam reads this feature at all, at any symbol value - see gate.py's import list, and
        # note that this is why VIRTIO_PCI's UNLESS_INITRD below is manifest metadata today rather
        # than a warning: the value renders into the contract document and nothing evaluates it.
        (
            # SERIAL_8250_CONSOLE is settable only once SERIAL_8250 is built in, so the
            # prerequisite is its own AND-ed clause (ADR-0544 rule 1): a clause can report that
            # SERIAL_8250 is missing, where the summary's closing sentence can only advise it.
            # That sentence stays - it explains *why* to a reader, and the clause is what a
            # config diff acts on.
            #
            # HVC_CONSOLE's own `depends on PPC_PSERIES` gets no such clause, deliberately:
            # kdive boots ppc64le only as machine="pseries", so the arch tag below already
            # says everything a second clause would, against a dependency no kdive guest can
            # fail. A prerequisite clause earns its place when the agent can get it wrong.
            Clause(frozenset({"SERIAL_8250"}), BuiltIn.REQUIRED, _X86_ONLY),
            Clause(frozenset({"SERIAL_8250_CONSOLE"}), arches=_X86_ONLY),
            Clause(frozenset({"HVC_CONSOLE"}), arches=_PPC64LE_ONLY),
            Clause(frozenset({"VIRTIO_PCI"}), BuiltIn.UNLESS_INITRD),
        ),
    ),
)

_BY_ID: dict[str, FeatureRequirement] = {f.feature: f for f in FEATURE_REQUIREMENTS}


def feature_requirement(feature_id: str) -> FeatureRequirement:
    return _BY_ID[feature_id]


def _manifest_clause(clause: Clause) -> dict[str, JsonValue]:
    """Render one clause as the manifest's object element, omitting keys at their defaults."""
    # Inner comprehension (not bare sorted()) widens list[str] -> list[JsonValue].
    element: dict[str, JsonValue] = {"symbols": [symbol for symbol in sorted(clause.symbols)]}
    if clause.built_in is not BuiltIn.NOT_REQUIRED:
        element["built_in"] = clause.built_in.value
    if clause.arches is not None:
        element["arch"] = [arch for arch in sorted(clause.arches)]
    return element


def enforcement_legend() -> dict[str, JsonValue]:
    """The served definition of every :class:`Enforcement` value (ADR-0546 §3).

    Rendered from the enum rather than written out beside the document, so a value cannot reach an
    agent without its definition. Shipped inside ``feature_config_requirements`` for the same
    reason the definition exists at all: a reader who has the flag has the legend.
    """
    return {value.value: ENFORCEMENT_LEGEND[value] for value in Enforcement}


def feature_manifest() -> list[dict[str, JsonValue]]:
    manifest: list[dict[str, JsonValue]] = []
    for f in FEATURE_REQUIREMENTS:
        # A clause renders as an object, not a bare list (ADR-0544 §6, #1854), which is what let
        # the built-in requirement (#1860) and then the arch scope (#1859) land as keys beside
        # `symbols` with one element-shape change between them. Both are omitted at their
        # defaults, so an unconstrained clause stays as small as it was.
        requirements: list[JsonValue] = [_manifest_clause(clause) for clause in f.advertised]
        entry: dict[str, JsonValue] = {
            "feature": f.feature,
            "summary": f.summary,
            # Replaces the `gated` bool at schema_version 3 (ADR-0546 §1, #1867): the bool said
            # whether kdive refuses, and an agent read it as whether the feature is optional.
            "enforcement": f.enforcement.value,
            "requirements": requirements,
        }
        # The refusal set, rendered in the same clause shape and present only when there is one.
        # It is not a flag on `requirements`, because the two do not correspond one-to-one:
        # crash_capture advertises {KEXEC} and {KEXEC_FILE} separately and gates them as one
        # OR-group, so a per-clause flag would claim omitting KEXEC alone refuses (ADR-0546 §2).
        if f.gate_required:
            entry["refuses_on"] = [_manifest_clause(clause) for clause in f.gate_required]
        manifest.append(entry)
    return manifest
