"""Allocation lease, queue, and orphaned-`active` repair for the reconciler.

The orphaned-`active` reaper answers one question — does this `active` allocation still back
live work? — for two shapes of leak: a System that reached a terminal state (or was never
created) with the allocation never released (ADR-0109), and a `crashed` System whose crash
investigation was abandoned mid-flight (ADR-0480). The second needs an activity signal rather
than a state test, because `crashed` is exactly the state a *live* investigation sits in.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.capacity.state import (
    AllocationState,
    DebugSessionState,
    JobState,
    SystemState,
)
from kdive.domain.operations.jobs import JobKind
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation import promotion as allocation_promotion
from kdive.services.allocation import release as allocation_release
from kdive.services.allocation.admission.metrics import AdmissionMetrics

_log = logging.getLogger(__name__)

SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"

DEFAULT_QUEUE_MAX_WAIT = timedelta(hours=24)

_TERMINAL_ALLOCATION_STATES = (
    AllocationState.RELEASED,
    AllocationState.EXPIRED,
    AllocationState.FAILED,
)
_EXPIRED_ALLOCATION_STATE = AllocationState.EXPIRED
_EXPIRED_ALLOCATION_STATE_VALUE = _EXPIRED_ALLOCATION_STATE.value
_TERMINAL_ALLOCATION_STATE_VALUES = tuple(state.value for state in _TERMINAL_ALLOCATION_STATES)

_ACTIVE_JOB_STATES = (JobState.QUEUED, JobState.RUNNING)
_CAPTURE_VMCORE_JOB_KIND_VALUE = JobKind.CAPTURE_VMCORE.value
_ACTIVE_JOB_STATE_VALUES = tuple(state.value for state in _ACTIVE_JOB_STATES)
_RUNNING_JOB_STATE_VALUE = JobState.RUNNING.value

_ACTIVE_ALLOCATION_STATE_VALUE = AllocationState.ACTIVE.value
_DETACHED_DEBUG_SESSION_STATE_VALUE = DebugSessionState.DETACHED.value

# A System in one of these states is "live" — it keeps its allocation legitimately occupied.
# This is the complement of admission's `_NON_TERMINAL_SYSTEM` (provisioning/ready/reprovisioning/
# restoring/paused/crashing/crashed); a `crashing` (mid-force_crash) or `crashed` System whose
# allocation backs an in-progress crash investigation is live, NOT orphaned. Keep this in step
# with `_NON_TERMINAL_SYSTEM` when SystemState gains a value. Mirrors the sibling
# `reconciler.systems._ORPHANED_SYSTEM_TERMINAL_STATES`.
#
# `crashed` is the one *conditionally* live member: it stays live only while its crash
# investigation shows activity (`_CRASHED_SYSTEM_IDLE_SQL`, ADR-0480, #1628). A `crashed` System
# still occupies a quota slot in every state-keyed set, including `_NON_TERMINAL_SYSTEM` — the
# exception here is about abandonment over time, not about the state's meaning.
_LIVE_SYSTEM_STATES = (
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.RESTORING,  # mid snapshot-revert: a live host domain (ADR-0378)
    SystemState.PAUSED,  # start_paused restore: suspended guest, still a live domain (ADR-0378)
    SystemState.CRASHING,
    SystemState.CRASHED,
)
_LIVE_SYSTEM_STATE_VALUES = tuple(state.value for state in _LIVE_SYSTEM_STATES)
_CRASHED_SYSTEM_STATE_VALUE = SystemState.CRASHED.value

# An `active` allocation whose System turned terminal (or is absent) is reclaimed only after
# its row has been settled this long, a belt-and-suspenders guard against the narrow window of
# a concurrent mid-provision write against the same allocation (ADR-0109). Mirrors the 2-min
# `DEFAULT_DEBUG_SESSION_STALE_AFTER` "settled long enough to be safe" precedent.
DEFAULT_ORPHANED_ACTIVE_GRACE = timedelta(minutes=2)

# How long a `crashed` System's crash investigation must be *silent* before its allocation is
# treated as abandoned rather than live (ADR-0480, #1628). Deliberately an order of magnitude
# above `DEFAULT_ORPHANED_ACTIVE_GRACE`: that window guards a read-then-act race measured in
# seconds, this one has to outlast an agent's think time between two capture methods on the same
# crashed guest. Deliberately far below the 4h `lease_expiry` that is the only thing reclaiming
# these slots today. Raise `ReconcileConfig.crashed_idle_grace` on a host where investigations
# routinely idle longer; lower it on a tight-cap host where a stranded slot denies real work.
DEFAULT_CRASHED_IDLE_GRACE = timedelta(minutes=30)

# The evidence that a `crashed` System's investigation is **abandoned**, not in progress. The
# central kdive workflow (force_crash -> capture -> analyze -> teardown) legitimately parks an
# allocation on a `crashed` System, so state alone cannot tell the two apart; three activity
# signals can, and all three must be silent:
#   1. the System row itself has not changed within the window (`crashed` is stamped by the
#      force_crash finalize, so this clock starts at the crash);
#   2. no job naming the System is active (`queued`/`running`) or was touched within the window
#      — capture_vmcore, power, teardown all carry `payload.system_id`;
#   3. no DebugSession on any of the System's Runs is non-terminal (`attach`/`live`) or was
#      touched within the window — a drgn/gdb session is the analysis half of the workflow.
# Any one signal firing keeps the allocation live, so a genuinely in-progress investigation is
# preserved for as long as it keeps producing DB activity.
#
# The job signal counts only jobs the reconciler did **not** author. `sweep_console_rotation`
# enqueues a fresh `console_rotate` job for every live local System — `crashed` included — on
# every pass, forever, so counting it would make signal 2 permanently true and this whole repair
# unreachable on the local provider. Keyed on the authorizing principal rather than an excluded
# kind list so a future reconciler-issued job kind is excluded automatically; `IS DISTINCT FROM`
# so a row with no recorded principal counts as activity (the preserving direction).
_CRASHED_SYSTEM_IDLE_SQL = """
        s.updated_at < now() - %(crashed_idle_grace)s
        AND NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.payload->>'system_id' = s.id::text
              AND j.authorizing->>'principal' IS DISTINCT FROM %(reconciler_principal)s
              AND (j.state = ANY(%(active_job_states)s)
                   OR j.updated_at >= now() - %(crashed_idle_grace)s)
        )
        AND NOT EXISTS (
            SELECT 1 FROM debug_sessions ds JOIN runs r ON r.id = ds.run_id
            WHERE r.system_id = s.id
              AND (ds.state <> %(detached_session_state)s
                   OR ds.updated_at >= now() - %(crashed_idle_grace)s)
        )
"""


def _live_system_exists_sql(allocation_ref: LiteralString) -> LiteralString:
    """Build the one definition of "this allocation still has a live System".

    ``allocation_ref`` is the SQL *expression* naming the allocation id — the correlated
    ``a.id`` in the candidate scan, a bound placeholder in the under-lock re-check. It is typed
    ``LiteralString`` so no runtime value can reach it. Both call sites share this one fragment
    so the unlocked pre-filter and the locked re-check can never drift apart.
    """
    return (
        "EXISTS (SELECT 1 FROM systems s "
        f"        WHERE s.allocation_id = {allocation_ref} "
        "          AND s.state = ANY(%(live_system_states)s) "
        "          AND NOT (s.state = %(crashed_system_state)s AND ("
        f"{_CRASHED_SYSTEM_IDLE_SQL}))"
        ")"
    )


# `crashed_idle` is carried purely so the reclaim can say *which* leak it just closed. The
# crashed-idle arm is the one that can end an investigation an operator still believed in, so
# "your guest is gone" must be greppable and must name the window that decided it.
_ORPHANED_ACTIVE_CANDIDATES_SQL = (
    "SELECT a.id, a.project, "
    "  EXISTS (SELECT 1 FROM systems s2 "
    "          WHERE s2.allocation_id = a.id AND s2.state = %(crashed_system_state)s) "
    "  AS crashed_idle "
    "FROM allocations a "
    "WHERE a.state = %(active_allocation_state)s "
    "  AND a.updated_at < now() - %(grace)s "
    "  AND NOT " + _live_system_exists_sql("a.id")
)

_HAS_LIVE_SYSTEM_SQL = "SELECT 1 WHERE " + _live_system_exists_sql("%(allocation_id)s")


def _liveness_params(crashed_idle_grace: timedelta) -> dict[str, object]:
    """The named parameters :func:`_live_system_exists_sql` binds, for either call site."""
    return {
        "live_system_states": list(_LIVE_SYSTEM_STATE_VALUES),
        "crashed_system_state": _CRASHED_SYSTEM_STATE_VALUE,
        "crashed_idle_grace": crashed_idle_grace,
        "active_job_states": list(_ACTIVE_JOB_STATE_VALUES),
        "detached_session_state": _DETACHED_DEBUG_SESSION_STATE_VALUE,
        "reconciler_principal": SYSTEM_RECONCILER_PRINCIPAL,
    }


def reap_queue_timeouts_for(
    queue_max_wait: timedelta, metrics: AdmissionMetrics | None = None
) -> Callable[[AsyncConnection], Awaitable[int]]:
    """Bind the max-wait window (+ metrics) into the queue_timeout reaper for isolated run."""

    async def _reap(conn: AsyncConnection) -> int:
        return await allocation_promotion.reap_queue_timeouts(conn, queue_max_wait, metrics)

    return _reap


async def sweep_expired_allocations(conn: AsyncConnection) -> int:
    """Reclaim allocations whose lease window has elapsed (ADR-0036, ADR-0040)."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, project FROM allocations "
            "WHERE state <> ALL(%s) AND lease_expiry IS NOT NULL AND lease_expiry < now()",
            (list(_TERMINAL_ALLOCATION_STATE_VALUES),),
        )
        candidates = await cur.fetchall()
    reclaimed = 0
    for candidate in candidates:
        try:
            if await _expire_one(conn, candidate["id"], candidate["project"]):
                reclaimed += 1
        except Exception:  # noqa: BLE001 - one allocation must not starve the rest
            _log.warning(
                "reconciler: expiring allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reclaimed


async def _expire_one(conn: AsyncConnection, allocation_id: UUID, project: str) -> bool:
    """Move one allocation to ``expired`` and reconcile under PROJECT -> ALLOCATION."""
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        alloc = await ALLOCATIONS.get(conn, allocation_id)
        if alloc is None or alloc.state in _TERMINAL_ALLOCATION_STATES:
            return False
        if not await _lease_elapsed(conn, allocation_id):
            return False
        alloc = await accounting.stamp_active_ended(conn, alloc, datetime.now(UTC))
        await ALLOCATIONS.update_state(conn, allocation_id, _EXPIRED_ALLOCATION_STATE)
        await audit.record_system(
            conn,
            principal=SYSTEM_RECONCILER_PRINCIPAL,
            event=audit.AuditEvent(
                tool="reconciler.sweep_expired",
                object_kind="allocations",
                object_id=allocation_id,
                transition=f"{alloc.state.value}->{_EXPIRED_ALLOCATION_STATE_VALUE}",
                args={"allocation_id": str(allocation_id)},
                project=project,
            ),
        )
        await accounting.reconcile(conn, alloc)
    _log.info("reconciler: allocation %s lease expired -> expired + reconciled", allocation_id)
    return True


async def _lease_elapsed(conn: AsyncConnection, allocation_id: UUID) -> bool:
    """Report whether the allocation's lease is still elapsed under the allocation lock."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT lease_expiry IS NOT NULL AND lease_expiry < now() "
            "FROM allocations WHERE id = %s",
            (allocation_id,),
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


async def reap_orphaned_active_allocations(
    conn: AsyncConnection,
    grace: timedelta = DEFAULT_ORPHANED_ACTIVE_GRACE,
    crashed_idle_grace: timedelta = DEFAULT_CRASHED_IDLE_GRACE,
) -> int:
    """Release each `active` allocation whose System is terminal, absent, or abandoned-crashed.

    A failed/interrupted lifecycle run leaves an allocation `active` while its single System
    reached a terminal state (`torn_down`/`failed`) — the teardown job never releases the
    allocation — so it permanently holds its host-cap slot (`active` is in admission's
    `OCCUPYING` set), wedging a `cap=1` host (ADR-0109, #371). A run that aborts *between*
    crashing its System and releasing the allocation strands the slot the same way, except the
    System sits in `crashed` (#1628): `crashed` is a live state, so the terminal-System
    predicate alone never reclaims it and only the 4h lease eventually does.

    Candidates are read with no lock: `active`, settled past `grace`, and with no `live` System
    — a `systems` row in `_LIVE_SYSTEM_STATES`, except a `crashed` one whose investigation has
    been silent for `crashed_idle_grace` (`_CRASHED_SYSTEM_IDLE_SQL`). Each candidate is then
    reclaimed under `PROJECT -> ALLOCATION` (re-checked under the lock), in its own
    transaction, isolated so one failure never starves the rest.
    """
    params = _liveness_params(crashed_idle_grace) | {
        "active_allocation_state": _ACTIVE_ALLOCATION_STATE_VALUE,
        "grace": grace,
    }
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ORPHANED_ACTIVE_CANDIDATES_SQL, params)
        candidates = await cur.fetchall()
    reclaimed = 0
    for candidate in candidates:
        try:
            if await _reclaim_orphaned_active(
                conn,
                candidate["id"],
                candidate["project"],
                crashed_idle_grace,
                crashed_idle=candidate["crashed_idle"],
            ):
                reclaimed += 1
        except Exception:  # noqa: BLE001 - one allocation must not starve the rest
            _log.warning(
                "reconciler: reclaiming orphaned active allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reclaimed


async def _reclaim_orphaned_active(
    conn: AsyncConnection,
    allocation_id: UUID,
    project: str,
    crashed_idle_grace: timedelta,
    *,
    crashed_idle: bool = False,
) -> bool:
    """Re-check the orphaned-active predicate under the allocation lock, then release.

    Returns True only when the allocation was released this pass. The no-live-System check is
    re-run as a `precondition` **under** the `PROJECT -> ALLOCATION` lock (held by
    `reclaim_under_lock`, which also runs the release transition), so a System (re)created
    between the candidate read and the lock is not reclaimed — closing the read-then-act gap.
    The re-check runs the *same* liveness SQL, so a crash investigation that resumed between the
    candidate read and the lock (a capture job enqueued, a debug session attached) is preserved
    too. A concurrent release/expiry that already moved the allocation terminal yields a
    non-`released` outcome, which is skipped (idempotent re-run).

    ``crashed_idle`` only selects the log line: the crashed-idle arm is the one that can end an
    investigation an operator still believed in, so it says so and names the knob.
    """

    async def _still_orphaned(locked: AsyncConnection) -> bool:
        return not await _has_live_system(locked, allocation_id, crashed_idle_grace)

    outcome = await allocation_release.reclaim_under_lock(
        conn,
        _system_audit_writer(allocation_id),
        allocation_id,
        project=project,
        precondition=_still_orphaned,
    )
    if not outcome.released:
        return False
    if crashed_idle:
        _log.info(
            "reconciler: allocation %s released — its crashed System showed no investigation "
            "activity for %s (ADR-0480); teardown of the crashed guest follows. Raise "
            "ReconcileConfig.crashed_idle_grace if investigations here idle longer",
            allocation_id,
            crashed_idle_grace,
        )
    else:
        _log.info(
            "reconciler: orphaned active allocation %s released (System terminal/absent)",
            allocation_id,
        )
    return True


def _system_audit_writer(allocation_id: UUID) -> allocation_release.AuditWriter:
    """A guard-exempt writer: `record_system` under the reconciler principal (no membership)."""

    async def _write(conn: AsyncConnection, event: audit.AuditEvent) -> None:
        await audit.record_system(conn, principal=SYSTEM_RECONCILER_PRINCIPAL, event=event)

    return _write


async def _has_live_system(
    conn: AsyncConnection, allocation_id: UUID, crashed_idle_grace: timedelta
) -> bool:
    """True if the allocation has any System that is live (non-terminal, and not crashed-idle)."""
    async with conn.cursor() as cur:
        await cur.execute(
            _HAS_LIVE_SYSTEM_SQL,
            _liveness_params(crashed_idle_grace) | {"allocation_id": allocation_id},
        )
        return await cur.fetchone() is not None


async def has_active_capture_job(conn: AsyncConnection, system_id: UUID) -> bool:
    """True if ``system_id`` has a running Run-addressed capture job (ADR-0557)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM jobs j "
            "JOIN runs r ON r.id::text = j.payload->>'run_id' "
            "WHERE j.kind = %s AND j.state = %s AND r.system_id = %s LIMIT 1",
            (
                _CAPTURE_VMCORE_JOB_KIND_VALUE,
                _RUNNING_JOB_STATE_VALUE,
                system_id,
            ),
        )
        return await cur.fetchone() is not None
