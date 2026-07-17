"""Derive a Run's live-health signal for ``runs.get`` (ADR-0373, #1237).

A guest that livelocks *after* a ready boot still reads ``boot_outcome=ready`` and produces no
crash signature (``watch_for_crash=not_fired``), so neither existing signal distinguishes it from a
healthy guest. This module folds two independent, read-time signals into one ``liveness`` block: a
black-box console-storm heuristic over the current redacted console tail, and the latest
``check_ssh_reachable`` probe verdict. Both derive at read time — no persistence, no migration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.console.console_evidence import redacted_console_tail
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue

STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_UNKNOWN = "unknown"

# The redacted-console window the storm heuristic scans. Large enough that a runaway retry loop's
# repeated line clears the repetition threshold, small enough that the status read stays cheap.
_STORM_TAIL_CHARS = 4096

# printk rate-limiting reports it dropped a message flood only under a storm, so its presence
# alone flags one; the other signatures need repetition to clear the noise floor.
_SUPPRESSION_MARKER = "callbacks suppressed"

# Livelock / OOM-storm hallmarks (#1237) plus the RCU-stall and OOM-killer lines that accompany
# them. Matched case-insensitively as literal substrings against the redacted console tail.
_STORM_SIGNATURES = (
    _SUPPRESSION_MARKER,
    "vm_fault_oom",
    "soft lockup",
    "hung task",
    "hung_task",
    "detected stall",
    "out of memory",
)

# A single benign line (e.g. one app OOM) stays below this; a storm repeats its line many times.
_STORM_MIN_HITS = 3


@dataclass(frozen=True, slots=True)
class Liveness:
    """The combined liveness verdict surfaced as ``runs.get`` ``data.liveness`` (ADR-0373)."""

    state: str
    console_storm: bool
    ssh_reachable: bool | None
    checked_at: str | None

    def as_data(self) -> dict[str, JsonValue]:
        """Render the liveness block for the ``runs.get`` envelope ``data`` slot."""
        return {
            "state": self.state,
            "console_storm": self.console_storm,
            "ssh_reachable": self.ssh_reachable,
            "checked_at": self.checked_at,
        }


def detect_console_storm(console_tail: str | None) -> bool:
    """Return whether ``console_tail`` shows a runaway printk / OOM-retry storm (ADR-0373).

    Fires when the printk rate-limit marker is present (the kernel self-reports a dropped message
    flood) or the combined count of storm signatures reaches ``_STORM_MIN_HITS``. An empty or
    ``None`` tail is not a storm.
    """
    if not console_tail:
        return False
    haystack = console_tail.lower()
    if _SUPPRESSION_MARKER in haystack:
        return True
    hits = sum(haystack.count(signature) for signature in _STORM_SIGNATURES)
    return hits >= _STORM_MIN_HITS


def derive_state(*, console_storm: bool, ssh_reachable: bool | None, console_read: bool) -> str:
    """Combine the two signals into a ``healthy`` / ``degraded`` / ``unknown`` state (ADR-0373).

    ``degraded`` when the console storms or SSH is unreachable after a ready boot; ``unknown`` when
    no console was readable and SSH was never probed (no signal to judge); ``healthy`` otherwise.
    """
    if console_storm or ssh_reachable is False:
        return STATE_DEGRADED
    if not console_read and ssh_reachable is None:
        return STATE_UNKNOWN
    return STATE_HEALTHY


def _parse_ssh_verdict(result_ref: str | None) -> tuple[bool | None, str | None]:
    """Extract ``(reachable, checked_at)`` from a ``check_ssh_reachable`` ``result_ref`` verdict.

    Returns ``(None, None)`` when the verdict is absent, unparsable, or missing a boolean
    ``reachable`` — never a fabricated ``False``.
    """
    if result_ref is None:
        return None, None
    try:
        verdict = json.loads(result_ref)
    except ValueError:  # JSONDecodeError subclasses ValueError; result_ref is always a str here
        return None, None
    if not isinstance(verdict, dict):
        return None, None
    reachable = verdict.get("reachable")
    if not isinstance(reachable, bool):
        return None, None
    checked_at = verdict.get("checked_at")
    return reachable, checked_at if isinstance(checked_at, str) else None


async def _latest_ssh_verdict(
    conn: AsyncConnection, system_id: UUID
) -> tuple[bool | None, str | None]:
    job = await queue.latest_succeeded_job_for_system(conn, JobKind.CHECK_SSH_REACHABLE, system_id)
    if job is None:
        return None, None
    return _parse_ssh_verdict(job.result_ref)


async def derive_liveness(
    conn: AsyncConnection, system_id: UUID, secret_registry: SecretRegistry
) -> Liveness:
    """Read both signals for ``system_id`` and combine them into a :class:`Liveness` (ADR-0373).

    Best-effort: an unreadable console yields ``console_storm=False`` with no signal, and an
    un-probed guest yields ``ssh_reachable=None``; the state derivation degrades gracefully.
    """
    console_tail = await redacted_console_tail(
        system_id, secret_registry, max_chars=_STORM_TAIL_CHARS
    )
    console_storm = detect_console_storm(console_tail)
    ssh_reachable, checked_at = await _latest_ssh_verdict(conn, system_id)
    state = derive_state(
        console_storm=console_storm,
        ssh_reachable=ssh_reachable,
        console_read=console_tail is not None,
    )
    return Liveness(
        state=state,
        console_storm=console_storm,
        ssh_reachable=ssh_reachable,
        checked_at=checked_at,
    )
