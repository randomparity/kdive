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
SYSRQ = "sysrq"


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
        "rootfs_mount",
        "Mount the kdive squashfs+overlay rootfs the guest boots from.",
        _plain(
            "SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP", "XFS_FS", "XFS_POSIX_ACL"
        ),
    ),
    FeatureRequirement(
        CRASH_CAPTURE,
        "Reserve a crashkernel and capture a vmcore via kdump.",
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
        "ikconfig",
        "Read the running kernel's own config back via /proc/config.gz.",
        _plain("IKCONFIG", "IKCONFIG_PROC"),
    ),
    FeatureRequirement(
        "debuginfo",
        "Resolve symbols for gdb/drgn debugging (build with DWARF or BTF).",
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
        _plain("SERIAL_8250_CONSOLE", "VIRTIO_BLK", "VIRTIO_PCI"),
    ),
)

_BY_ID: dict[str, FeatureRequirement] = {f.feature: f for f in FEATURE_REQUIREMENTS}


def feature_requirement(feature_id: str) -> FeatureRequirement:
    """Return the registry entry for ``feature_id`` (raises ``KeyError`` if unknown)."""
    return _BY_ID[feature_id]


def feature_manifest() -> list[dict[str, JsonValue]]:
    """Render the advertised manifest (advisory): one entry per feature, ``advertised`` only."""
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
