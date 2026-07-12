"""Config checks over a Run's uploaded kernel config (ADR-0318, ADR-0322, ADR-0330).

Three consumers share :func:`load_effective_config` here:

- the crash-capture arming seams (install crashkernel reservation, kdump vmcore fetch) **refuse**
  when the config provably lacks the crash-capture symbols;
- the drgn-live debug seams (``debug.start_session``, live ``introspect.*``) **warn** — never
  refuse — when the config provably lacks debuginfo and no host ``vmlinux`` was uploaded; and
- ``runs.complete_build`` **warns** — never refuses — when the config provably lacks the
  boot-required ``rootfs_mount`` symbols the guest needs to mount its root filesystem.

All fail open (an absent/unreadable/degenerate config yields ``None``: arm/attach/complete as
today). Each seam formats its own envelope from the returned payload.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    ROOTFS_MOUNT,
    feature_requirement,
)
from kdive.kernel_config.support import (
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)
from kdive.serialization import JsonValue

CRASH_CONFIG_REASON = "kernel_missing_crash_config"
_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_* (see artifacts.feature_config_requirements)"
)

MISSING_BOOT_CONFIG_REASON = "kernel_missing_boot_config"
_ROOTFS_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_* so the guest can mount its ext4 root filesystem "
    "and boot (see artifacts.feature_config_requirements)"
)

MISSING_DEBUGINFO_REASON = "missing_debuginfo"
# In-guest drgn-live resolves symbols from the running kernel's BTF (/sys/kernel/btf/vmlinux).
# DWARF built into the kernel .config does NOT help: the DWARF-carrying vmlinux is not on the guest
# rootfs. The only other in-guest source is a host vmlinux uploaded as the Run's debuginfo_ref, so
# the warning keys on BTF specifically and is suppressed when a vmlinux was uploaded.
_BTF_SYMBOL = "DEBUG_INFO_BTF"
_DEBUGINFO_REMEDIATION = (
    "enable CONFIG_DEBUG_INFO_BTF (in-guest drgn reads BTF from /sys/kernel/btf), or upload a "
    "matching vmlinux, so drgn can resolve symbols (see artifacts.feature_config_requirements)"
)


async def crash_capture_refusal(conn: AsyncConnection, run_id: UUID) -> dict[str, JsonValue] | None:
    """Refusal ``details`` if the Run's uploaded config lacks crash-capture symbols, else ``None``.

    Returns ``None`` (arm as today) when no config was uploaded or it cannot be read/trusted
    (:func:`load_effective_config` fails open), and when the config fully supports crash capture.
    Otherwise returns ``{reason, missing, remediation}`` — the shared payload both crash seams
    spread into their own refusal envelope, so the reason code and remediation cannot drift.
    """
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_clauses(config, feature_requirement(CRASH_CAPTURE))
    if not unmet:
        return None
    missing: list[JsonValue] = list(missing_symbols(unmet))
    return {"reason": CRASH_CONFIG_REASON, "missing": missing, "remediation": _REMEDIATION}


async def rootfs_mount_warning(conn: AsyncConnection, run_id: UUID) -> dict[str, JsonValue] | None:
    """Non-fatal ``kernel_missing_boot_config`` warning for ``runs.complete_build``, or ``None``.

    Returns ``None`` (complete as today) when no ``effective_config`` was uploaded or it cannot be
    read/trusted (:func:`load_effective_config` fails open), and when the config enables every
    ``rootfs_mount`` boot symbol. Otherwise returns ``{reason, missing, remediation}`` — the payload
    the complete_build handler spreads into its success ``data`` so a kernel that cannot mount its
    root filesystem is no longer silently completed. Keys on the ``rootfs_mount`` *advertised*
    clauses (the feature is never gated); warns, never refuses — the upload always succeeds.
    """
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_advertised_clauses(config, feature_requirement(ROOTFS_MOUNT))
    if not unmet:
        return None
    missing: list[JsonValue] = list(missing_symbols(unmet))
    return {
        "reason": MISSING_BOOT_CONFIG_REASON,
        "missing": missing,
        "remediation": _ROOTFS_REMEDIATION,
    }


async def debuginfo_warning(
    conn: AsyncConnection, run_id: UUID, *, has_uploaded_vmlinux: bool
) -> dict[str, JsonValue] | None:
    """Non-fatal ``missing_debuginfo`` warning for a drgn-live seam, or ``None`` (ADR-0322).

    Returns ``None`` (no warning) when a host ``vmlinux``/``debuginfo_ref`` was uploaded (drgn can
    resolve via that vmlinux), when no ``effective_config`` was uploaded or it cannot be
    read/trusted (:func:`load_effective_config` fails open), and when the config provably enables
    BTF (the in-guest symbol source). Otherwise returns ``{reason, missing, remediation}`` — the
    payload the drgn-live seams spread into their response ``data`` so a blind session is no longer
    silently successful. Warns, never refuses: the uploaded-``vmlinux`` path must keep working.
    """
    if has_uploaded_vmlinux:
        return None
    config = await load_effective_config(conn, run_id)
    if config is None or config.is_enabled(_BTF_SYMBOL):
        return None
    return {
        "reason": MISSING_DEBUGINFO_REASON,
        "missing": [_BTF_SYMBOL],
        "remediation": _DEBUGINFO_REMEDIATION,
    }
