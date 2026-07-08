"""Always-on platform kernel-config requirements (ADR-0316).

One source of truth for the symbols every kdive server build must carry, shared by the build
guard (``_validate_final_config``) and the agent-facing surface (``buildconfig.get``). The
universal set is scoped to the rootfs-*mount* symbols every System needs regardless of capture
method and that ``olddefconfig`` will not auto-select; capture-method symbols live in
per-method ``profile_requirements``.
"""

from __future__ import annotations

from kdive.components.requirements import ConfigRequirements

# Exact `=y` requirements: rootfs/boot-mount symbols. Not auto-selected by olddefconfig.
PLATFORM_REQUIRED_CONFIG = ConfigRequirements(
    required={
        "CONFIG_SQUASHFS": "y",
        "CONFIG_SQUASHFS_ZSTD": "y",
        "CONFIG_OVERLAY_FS": "y",
        "CONFIG_BLK_DEV_LOOP": "y",
        "CONFIG_XFS_FS": "y",
    }
)

# Pre-existing always-on check, held here unchanged: crash-dump + the debuginfo OR-group.
REQUIRED_KERNEL_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)

PLATFORM_CONFIG_SYMBOL_MISSING = "platform_config_symbol_missing"


def platform_required_payload() -> dict[str, object]:
    """The surfaced platform requirement, derived from the constants the build guard enforces."""
    return {
        "all_of": dict(PLATFORM_REQUIRED_CONFIG.required),
        "any_of": [list(group) for group in REQUIRED_KERNEL_CONFIG],
    }


__all__ = [
    "PLATFORM_CONFIG_SYMBOL_MISSING",
    "PLATFORM_REQUIRED_CONFIG",
    "REQUIRED_KERNEL_CONFIG",
    "platform_required_payload",
]
