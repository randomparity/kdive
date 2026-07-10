"""Config checks over a Run's uploaded kernel config (ADR-0318, ADR-0322).

Two consumers share :func:`load_effective_config` here:

- the crash-capture arming seams (install crashkernel reservation, kdump vmcore fetch) **refuse**
  when the config provably lacks the crash-capture symbols; and
- the drgn-live debug seams (``debug.start_session``, live ``introspect.*``) **warn** — never
  refuse — when the config provably lacks debuginfo and no host ``vmlinux`` was uploaded.

Both fail open (an absent/unreadable/degenerate config yields ``None``: arm/attach as today). Each
seam formats its own envelope from the returned payload.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.requirements import CRASH_CAPTURE, DEBUGINFO, feature_requirement
from kdive.kernel_config.support import missing_symbols, unmet_advertised_clauses, unmet_clauses
from kdive.serialization import JsonValue

CRASH_CONFIG_REASON = "kernel_missing_crash_config"
_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_* (see artifacts.feature_config_requirements)"
)

MISSING_DEBUGINFO_REASON = "missing_debuginfo"
_DEBUGINFO_REMEDIATION = (
    "rebuild the kernel with DWARF or BTF debuginfo, or upload a matching vmlinux, so drgn can "
    "resolve symbols (see artifacts.feature_config_requirements)"
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


async def debuginfo_warning(
    conn: AsyncConnection, run_id: UUID, *, has_uploaded_vmlinux: bool
) -> dict[str, JsonValue] | None:
    """Non-fatal ``missing_debuginfo`` warning for a drgn-live seam, or ``None`` (ADR-0322).

    Returns ``None`` (no warning) when a host ``vmlinux``/``debuginfo_ref`` was uploaded (drgn can
    resolve via DWARF), when no ``effective_config`` was uploaded or it cannot be read/trusted
    (:func:`load_effective_config` fails open), and when the config provably carries DWARF/BTF
    debuginfo. Otherwise returns ``{reason, missing, remediation}`` — the payload the drgn-live
    seams spread into their response ``data`` so a blind session is no longer silently successful.
    Warns, never refuses: the uploaded-``vmlinux`` path must keep working.
    """
    if has_uploaded_vmlinux:
        return None
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_advertised_clauses(config, feature_requirement(DEBUGINFO))
    if not unmet:
        return None
    missing: list[JsonValue] = list(missing_symbols(unmet))
    return {
        "reason": MISSING_DEBUGINFO_REASON,
        "missing": missing,
        "remediation": _DEBUGINFO_REMEDIATION,
    }
