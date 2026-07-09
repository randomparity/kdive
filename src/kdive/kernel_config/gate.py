"""Config-gate helper for the crash-capture arming seams (ADR-0318).

The install crashkernel reservation and the kdump vmcore fetch both refuse when the Run's
uploaded kernel config provably lacks the crash-capture symbols. They differ only in the
refusal *vehicle* (install raises ``CategorizedError``; vmcore returns a ``ToolResponse``), so
this module owns the shared fetch → check → refusal-payload logic and each seam formats its own
envelope from the returned ``details``.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.requirements import CRASH_CAPTURE, feature_requirement
from kdive.kernel_config.support import missing_symbols, unmet_clauses
from kdive.serialization import JsonValue

CRASH_CONFIG_REASON = "kernel_missing_crash_config"
_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_* (see artifacts.feature_config_requirements)"
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
