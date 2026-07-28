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
        _plain(
            "KEXEC",
            "KEXEC_CORE",
            "KEXEC_FILE",
            "CRASH_DUMP",
            "VMCORE_INFO",
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
        (
            frozenset({"DEBUG_INFO"}),
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
        "Kernel Address Sanitizer instrumentation.",
        _plain("KASAN", "KASAN_INLINE"),
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
