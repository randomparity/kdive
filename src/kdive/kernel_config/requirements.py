"""Feature -> required CONFIG_* registry (ADR-0318).

Single source of truth for both the advertised manifest and the arming gate. Each feature
carries an ``advertised`` superset (guidance shown to the agent) and a deliberately narrower
``gate_required`` subset (what the gate refuses on). Each clause is an OR-group: satisfied
when any member symbol is enabled. Symbol names are bare (no ``CONFIG_`` prefix), matching
:func:`kdive.kernel_config.parse.parse_kernel_config`.
"""

from __future__ import annotations

from dataclasses import dataclass

from kdive.serialization import JsonValue

Clause = frozenset[str]

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
    """

    feature: str
    summary: str
    advertised: tuple[Clause, ...]
    gate_required: tuple[Clause, ...] = ()

    @property
    def gated(self) -> bool:
        return bool(self.gate_required)


def _plain(*symbols: str) -> tuple[Clause, ...]:
    return tuple(frozenset({s}) for s in symbols)


FEATURE_REQUIREMENTS: tuple[FeatureRequirement, ...] = (
    FeatureRequirement(
        ROOTFS_MOUNT,
        "Mount the root filesystem the guest boots from. Local-libvirt direct-kernel-boots a "
        "whole-disk ext4 qcow2 (root=/dev/vda, no initramfs); a remote or agent-uploaded rootfs "
        "is commonly XFS (RHEL-family base images). kdive does not know which family your guest "
        "uses, so it asks only that the kernel can mount at least one of them plus the virtio-blk "
        "root device - build in the one your rootfs actually uses.",
        (
            frozenset({"EXT4_FS", "XFS_FS"}),
            frozenset({"VIRTIO_BLK"}),
        ),
    ),
    FeatureRequirement(
        CRASH_CAPTURE,
        "Reserve a crashkernel and capture a vmcore via kdump. Guest-family-independent only: "
        "these symbols get the capture kernel loaded, not the vmcore written. A RHEL-family guest "
        "needs the crash_capture_rhel_guest set as well.",
        # KEXEC_CORE and VMCORE_INFO are advertised nowhere: both are bare prompt-less bools
        # (kernel/Kconfig.kexec:11 and :8) no fragment can set - olddefconfig discards them. They
        # also carry no signal here: KEXEC (:20) and KEXEC_FILE (:38) select KEXEC_CORE, and
        # CRASH_DUMP (:97) selects VMCORE_INFO (so does PROC_KCORE, fs/proc/Kconfig:32), so each
        # is off only when a selector this same entry already advertises is off. Both stay in
        # gate_required below - a derived symbol is still provably absent in a parsed .config.
        _plain(
            "KEXEC",
            "KEXEC_FILE",
            "CRASH_DUMP",
            "PROC_VMCORE",
            "FW_CFG_SYSFS",
            "RELOCATABLE",
            "RANDOMIZE_BASE",
        ),
        gate_required=(
            frozenset({"KEXEC_CORE"}),
            frozenset({"KEXEC", "KEXEC_FILE"}),  # either load syscall suffices
            frozenset({"CRASH_DUMP"}),
            frozenset({"PROC_VMCORE"}),
            frozenset({"VMCORE_INFO"}),
            frozenset({"FW_CFG_SYSFS"}),
            frozenset({"RELOCATABLE"}),
        ),
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
        "Read the running kernel's own config back via /proc/config.gz.",
        _plain("IKCONFIG", "IKCONFIG_PROC"),
    ),
    FeatureRequirement(
        "debuginfo",
        "Enable only for live drgn/gdb symbol resolution or offline vmcore analysis. Embeds "
        "DWARF tables in every .ko - can grow the module tree 10-50x and slow upload and "
        "install. Omit for boot-time crash reproducers and console-log investigations where no "
        "post-boot introspection is needed.",
        # DEBUG_INFO itself is not advertised: lib/Kconfig.debug:249 is a bare prompt-less bool no
        # fragment can set - olddefconfig discards it. It also carries no signal here: DWARF4 and
        # DWARF5 select it (:295, :307) and DEBUG_INFO_BTF (:398) sits inside `if DEBUG_INFO`
        # (:325-455), so DEBUG_INFO=n forces every member of the clause below off and that clause
        # reports the same kernel one symbol sooner.
        (
            frozenset({"DEBUG_INFO_DWARF5", "DEBUG_INFO_DWARF4", "DEBUG_INFO_BTF"}),
            frozenset({"DEBUG_KERNEL"}),
        ),
    ),
    FeatureRequirement(
        SYSRQ,
        "Inject magic SysRq diagnostics from the host.",
        _plain("MAGIC_SYSRQ"),
        gate_required=(frozenset({"MAGIC_SYSRQ"}),),
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
            frozenset({"KASAN"}),
            frozenset({"KASAN_GENERIC", "KASAN_SW_TAGS", "KASAN_HW_TAGS"}),
            frozenset({"STACKTRACE"}),
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
        (
            frozenset({"BPF_SYSCALL"}),
            frozenset({"PERF_EVENTS"}),
            frozenset({"KPROBE_EVENTS", "UPROBE_EVENTS"}),
            frozenset({"DEBUG_INFO_BTF"}),
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
            frozenset({"DEBUG_KERNEL"}),
            frozenset({"FAULT_INJECTION"}),
            frozenset(
                {
                    "FAILSLAB",
                    "FAIL_PAGE_ALLOC",
                    "FAIL_MAKE_REQUEST",
                    "FAIL_IO_TIMEOUT",
                    "FAIL_FUTEX",
                }
            ),
            frozenset({"FAULT_INJECTION_DEBUG_FS"}),
            frozenset({"DEBUG_FS"}),
            frozenset({"SYSFS"}),
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
        "Serial console + virtio devices the local-libvirt profile expects.",
        _plain("SERIAL_8250_CONSOLE", "VIRTIO_PCI"),
    ),
)

_BY_ID: dict[str, FeatureRequirement] = {f.feature: f for f in FEATURE_REQUIREMENTS}


def feature_requirement(feature_id: str) -> FeatureRequirement:
    return _BY_ID[feature_id]


def feature_manifest() -> list[dict[str, JsonValue]]:
    manifest: list[dict[str, JsonValue]] = []
    for f in FEATURE_REQUIREMENTS:
        # Inner comprehension (not bare sorted()) widens list[str] -> list[JsonValue].
        requirements: list[JsonValue] = [
            [symbol for symbol in sorted(clause)] for clause in f.advertised
        ]
        entry: dict[str, JsonValue] = {
            "feature": f.feature,
            "summary": f.summary,
            "gated": f.gated,
            "requirements": requirements,
        }
        manifest.append(entry)
    return manifest
