"""Orphaned remote host-dump volume cleanup lane."""

from __future__ import annotations

import logging
from datetime import timedelta

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, require_top_level_transaction, try_advisory_xact_lock
from kdive.providers.infra.reaping import DumpVolume, DumpVolumeReaper
from kdive.providers.shared.staging.host_dump_volume_leases import (
    has_live_host_dump_volume_lease,
    reap_stale_host_dump_volume_leases,
)
from kdive.reconciler.cleanup.reaping_common import (
    DEFAULT_LANE_BUDGET,
    ReapLaneOutcome,
    budget_unattempted,
    database_epoch,
    lane_deadline,
)
from kdive.reconciler.repairs.allocations import has_active_capture_job

_log = logging.getLogger(__name__)

DEFAULT_DUMP_VOLUME_GRACE = timedelta(minutes=30)

_budget_spent = budget_unattempted
_lane_deadline = lane_deadline
_now_epoch = database_epoch


async def reap_orphaned_dump_volumes(
    conn: AsyncConnection,
    reaper: DumpVolumeReaper,
    grace: timedelta,
    *,
    budget: timedelta = DEFAULT_LANE_BUDGET,
) -> ReapLaneOutcome:
    """Delete host_dump volumes orphaned by a non-graceful worker/host crash (ADR-0094, ADR-0562).

    Each candidate's final classification and its delete run in **one transaction holding
    ``(SYSTEM, system_id)``** — the lock ``hold_host_dump_volume_lease`` mints under. A capture
    therefore either declares itself before this pass classifies (and is seen), or blocks until the
    delete is done and then recreates a volume this pass has already passed over. Without that the
    guards are a state sample: ADR-0557 deliberately excludes a ``queued`` job, so a worker claiming
    the job between the check and the delete left the answer stale, and the capture's own
    delete-stale-then-dump pair put a **new** volume at the same deterministic name for the delete
    to resolve onto (#1955).

    Stale leases are collected first, ahead of the volume list, so a deployment whose reaper owns no
    volumes still drains rows a failed capture left behind.

    The transaction-free precondition is asserted **here**, not only per volume: the stale-lease
    collection runs before the volume list, and on a connection already in a transaction it
    would degrade to a savepoint that commits nothing. With an empty volume list the pass would then
    return ``0`` having silently discarded that work, never reaching the per-volume assertion.

    ``budget`` caps how long the lane keeps starting volumes (ADR-0565). Measured on the reconciler
    process's monotonic clock, per lane per pass, and consulted only **between** volumes — never
    while a delete is in flight, so it can end no transaction the provider is still mutating host
    state under. On violation the lane returns after the volume in flight completes, having
    attempted fewer than it listed; that is not a fault and is not counted, and the next pass
    re-derives the rest. The clock starts once the volume list is in hand: ``list_dump_volumes``
    is itself a whole-fleet fan-out, and a deadline started ahead of it would be spent before the
    loop on a degraded fleet and reclaim nothing, ever.

    A stalling host is bounded per call by the reaper's own connect gate; a host that accepts and
    then stalls is bounded only by this budget, which caps it at one volume per pass (#1981).

    Returns:
        A :class:`ReapLaneOutcome`: ``reaped`` is the number of volumes deleted; skips — a
        contended System, a live holder, a volume in the grace window, a volume whose identity
        changed, a volume no reachable host held — and a volume the budget left unattempted are
        not counted as reaped. ``budget_unattempted`` reports how many due volumes the budget
        stopped the lane from starting (#1982); ``0`` when the pass drained its worklist.
    """
    require_top_level_transaction(conn, "the host_dump orphan sweep")
    await reap_stale_host_dump_volume_leases(conn)
    volumes = await reaper.list_dump_volumes()
    if not volumes:
        return ReapLaneOutcome("dump-volume", 0)
    deadline = _lane_deadline(budget)
    # In its own transaction, so the connection is idle again afterwards. On the reconciler's
    # non-autocommit pool connection a bare `execute` opens a transaction that lives until the pool
    # takes the connection back, after which every per-volume `conn.transaction()` below would be a
    # savepoint that commits nothing and every `pg_advisory_xact_lock` would be held for the whole
    # pass rather than for one volume (ADR-0005; the same hazard `require_top_level_transaction`
    # exists for).
    async with conn.transaction():
        cutoff_epoch = await _now_epoch(conn) - grace.total_seconds()
    # Filtered before the loop rather than skipped inside it, so ``budget_unattempted`` counts
    # only volumes this pass would otherwise have deleted. Counting the trailing slice of the raw
    # list would report a backlog that is mostly volumes still inside the grace window, which the
    # lane was never going to touch (#1982).
    due = [volume for volume in volumes if volume.mtime_epoch_s < cutoff_epoch]
    reaped = 0
    for index, volume in enumerate(due):
        unattempted = _budget_spent(deadline, "dump-volume", remaining=len(due) - index)
        if unattempted is not None:
            return ReapLaneOutcome("dump-volume", reaped, unattempted)
        if await _delete_if_still_orphaned(conn, reaper, volume):
            reaped += 1
    return ReapLaneOutcome("dump-volume", reaped)


async def _delete_if_still_orphaned(
    conn: AsyncConnection, reaper: DumpVolumeReaper, volume: DumpVolume
) -> bool:
    """Re-classify ``volume`` and delete it, both under the System's advisory lock (ADR-0562).

    The acquire is a ``try``: a contended System is one a capture is declaring itself on now, so
    the volume is skipped and the next pass re-derives it. A blocking acquire would let one holder
    stall the lane for as long as it liked — the lane's pass budget (ADR-0565) caps how long the
    lane keeps *starting* volumes, not how long one blocked acquire waits — behind every lane
    placed after it. That is the trade ADR-0502 item 4 makes for the same reason. The skip is
    deliberately not a counted fault, so a wedged holder defers this System's volume on every pass
    while the count reads clean; the INFO line is the whole of the signal.

    A volume whose name carries no parseable System UUID has no lock to take and keeps its age-only
    classification; its delete is still identity-addressed.
    """
    require_top_level_transaction(conn, "the host_dump orphan sweep's per-volume fence")
    async with conn.transaction():
        if volume.system_id is not None:
            if not await try_advisory_xact_lock(conn, LockScope.SYSTEM, volume.system_id):
                _log.info(
                    "reconciler: system %s is locked by an active operation; deferring dump "
                    "volume %s to the next pass",
                    volume.system_id,
                    volume.name,
                )
                return False
            # Two holders, not one mechanism twice, but they overlap heavily: `dequeue` commits
            # `running` before the handler runs, so ADR-0557's predicate already covers the whole
            # interval from the claim to the last provider call. What the lease adds is that it is
            # keyed on the System directly. ADR-0557's predicate reaches the System only through
            # `runs.id::text = jobs.payload->>'run_id'`, so a Run row deleted mid-capture unfences a
            # live capture; the lease row does not depend on that join. What ADR-0557's predicate
            # adds is a `running` job whose *job lease* has lapsed — the queue has given up on it,
            # which makes the lease row no longer live, while its libvirt thread may still be
            # writing. Neither is redundant, and neither alone covers both.
            if await has_live_host_dump_volume_lease(conn, volume.system_id):
                return False
            if await has_active_capture_job(conn, volume.system_id):
                return False
        try:
            reclaimed = await reaper.delete_dump_volume(
                volume.name, expected_mtime_epoch_s=volume.mtime_epoch_s
            )
        except Exception:  # noqa: BLE001 - one volume failure must not starve the rest
            _log.warning(
                "reconciler: deleting orphaned dump volume %s failed; retry next pass",
                volume.name,
                exc_info=True,
            )
            return False
    if not reclaimed:
        # Either the provider found a different volume under this name than the one classified and
        # left it alone, or no reachable host held the name at all. Neither reclaimed anything, and
        # counting it would report reclamation that did not happen; the provider logs which case it
        # was, so nothing is added here.
        return False
    _log.info("reconciler: reaped orphaned host_dump volume %s", volume.name)
    return True
