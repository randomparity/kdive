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

from kdive.artifacts.read_model import effective_config_key
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

NO_EFFECTIVE_CONFIG_REASON = "no_effective_config_uploaded"
_NO_EFFECTIVE_CONFIG_REMEDIATION = (
    "upload the built kernel's effective_config alongside the build (artifacts.create_run_upload) "
    "so runs.complete_build can check the boot-critical EXT4_FS/VIRTIO_BLK symbols the guest needs "
    "to mount its root filesystem (see artifacts.feature_config_requirements)"
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

# The static config check above proves BTF is *advertised*, not that the running guest's drgn can
# load it. A drgn-live runtime symbol probe can still find the session blind (guest drgn cannot load
# the kernel's BTF); this is the distinct reason it emits, keyed on the same BTF symbol.
DEBUGINFO_UNLOADABLE_REASON = "debuginfo_unloadable"
_DEBUGINFO_UNLOADABLE_REMEDIATION = (
    "the in-guest drgn could not load the running kernel's BTF even though the config advertised "
    "it (a known limitation of some guest drgn builds); boot a BTF-capable guest image with a "
    "newer drgn, or upload a matching vmlinux (see artifacts.feature_config_requirements)"
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


async def missing_effective_config_nudge(
    conn: AsyncConnection, run_id: UUID
) -> dict[str, JsonValue] | None:
    """Non-blocking ``no_effective_config_uploaded`` nudge for ``runs.complete_build`` (ADR-0398).

    Returns ``{reason, remediation}`` when the Run has no ``effective_config`` artifact at all —
    the case :func:`rootfs_mount_warning` fails open on, so the EXT4_FS/VIRTIO_BLK boot-config
    advisory could never fire and the agent gets no signal it skipped the check. Returns ``None``
    once a config is present (uploaded), whether or not it is readable or complete: the warning
    path (present but missing symbols) and a plain success (present and complete) already cover
    those. Keys on artifact *presence* — a present-but-unreadable config is treated as provided,
    not absent. Advisory only: the completion always succeeds.
    """
    key = await effective_config_key(conn, run_id)
    if key is not None:
        return None
    return {
        "reason": NO_EFFECTIVE_CONFIG_REASON,
        "remediation": _NO_EFFECTIVE_CONFIG_REMEDIATION,
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


def debuginfo_unloadable_warning() -> dict[str, JsonValue]:
    """The runtime-probe ``debuginfo_unloadable`` warning payload (extends the static gate above).

    Emitted by the drgn-live introspect seams when a runtime symbol probe proves the in-guest drgn
    cannot resolve a stable kernel symbol, even though the static config check was silent (BTF
    advertised, or no config uploaded) and no host ``vmlinux`` was uploaded — the F1 case the
    ``.config``-based check cannot see. Names ``DEBUG_INFO_BTF`` and points at the same
    remediations, so a client keying on the ``{reason, missing, remediation}`` shape is unaffected.
    """
    return {
        "reason": DEBUGINFO_UNLOADABLE_REASON,
        "missing": [_BTF_SYMBOL],
        "remediation": _DEBUGINFO_UNLOADABLE_REMEDIATION,
    }
