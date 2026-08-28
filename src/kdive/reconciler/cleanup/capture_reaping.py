"""Orphaned traffic-capture cleanup lane."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import require_top_level_transaction, try_capture_job_fence
from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import JobKind
from kdive.providers.infra.reaping import (
    CaptureReaper,
    OrphanedCapture,
    dispatchable_capture_kinds,
)
from kdive.reconciler.cleanup.reaping_common import (
    DEFAULT_LANE_BUDGET,
    ReapLaneOutcome,
    budget_unattempted,
    lane_deadline,
)

_log = logging.getLogger(__name__)
_budget_spent = budget_unattempted
_lane_deadline = lane_deadline
#: How long a terminal ``capture_traffic`` row must sit untouched before the sweep considers it
#: (ADR-0556). Chosen to match :data:`DEFAULT_DUMP_VOLUME_GRACE` so an operator reasons about one
#: pacing number for provider host-state reclamation rather than two. There is no derived upper
#: bound available: a lapsed job lease means a dead or wedged worker, which says nothing about how
#: long a live one might still take. Settle is therefore pacing, not a safety fence — the per-job
#: ownership fence is what stops reclamation pre-empting a worker's late write.
DEFAULT_CAPTURE_SETTLE = timedelta(minutes=30)

#: Candidates considered per pass (ADR-0556 R8). With no lookback cutoff, an existing
#: deployment's entire capture history becomes eligible at the first pass after the migration, and
#: each candidate can cost one hypervisor connection. The backlog drains over several intervals
#: instead of opening thousands of connections at once.
DEFAULT_CAPTURE_REAP_BATCH = 25

#: The first retry delay after an attempt that did not reclaim, and the ceiling the doubling stops
#: at. A degraded provider therefore stops monopolising the batch, at the cost of waiting until a
#: row's deadline once the provider recovers.
DEFAULT_CAPTURE_RETRY_BASE = timedelta(minutes=5)
DEFAULT_CAPTURE_RETRY_CAP = timedelta(hours=6)

# Caps the doubling exponent so a row failing for a very long time cannot overflow the interval
# arithmetic; the LEAST against the cap already bounds the result long before this bites.
_BACKOFF_EXPONENT_CAP = 20
_TERMINAL_CAPTURE_STATE_VALUES = (
    JobState.SUCCEEDED.value,
    JobState.FAILED.value,
    JobState.CANCELED.value,
)
_CAPTURE_JOB_KIND_VALUE = JobKind.CAPTURE_TRAFFIC.value

# Selection for the ADR-0556 capture sweep. Notes on the shape, in the order the clauses read:
#
# * The Run join compares `rn.id::text` to the payload text rather than casting the payload to
#   uuid. A cast in a join condition can be evaluated before the `kind` filter narrows the rows,
#   and one malformed payload anywhere in `jobs` would then abort the whole pass.
# * `domain_name` prefers the stored column and falls back to the ADR-0111 derivation, exactly as
#   the capture handler resolves it. Re-deriving unconditionally would name the wrong domain for
#   any System that has a stored name.
# * The kind predicate is not optional (R3). Local-libvirt also wires a `TrafficCapturer`, so
#   without it a local row is handed to a remote reaper, fails host binding on every pass, and
#   buries the sweep's own failure signal under noise.
# * Evidence is either attempt-linked or cutover-covered, never absent. The first branch demands
#   the job's authoritative attempt be provably quiescent *and* publication-closed *and*
#   spool-disposed, so no fenced attempt can publish after a reap marks it complete. The second
#   accepts a job that has never had *any* supervised attempt, and then only when the durable
#   cutover generation is complete and the job was created no later than the committed cutoff —
#   the rows that predate supervision and can therefore never grow an attempt link. A job that
#   ever created an operation has left that population for good and is governed by its own
#   attempt's evidence, even across a retry. After the cutoff a missing link is fail-closed.
#
#   ADR-0556 describes that generation as recorded per provider kind. The table 0112 created and
#   0113 extended is a singleton, with one `complete` flag covering both kinds, so the predicate
#   reads the singleton and the kind match stays where it already is: the selection predicate
#   above. #1947 and #1948 must not assume a per-kind cutoff row exists.
# * Ordering leads with an explicit `(reap state exists)` discriminator instead of relying on
#   NULLs sorting a particular way, so an untouched row always precedes a just-failed one even
#   when that row's backoff expired first. Without it, one permanently failing old row would come
#   back every pass ahead of candidates that have never been tried.
_ORPHANED_CAPTURE_SQL = """
SELECT j.id AS job_id,
       s.id AS system_id,
       COALESCE(s.domain_name, 'kdive-' || s.id) AS domain_name,
       res.id AS resource_id,
       res.kind AS provider_kind,
       res.name AS resource_name
FROM jobs AS j
JOIN runs AS rn ON rn.id::text = j.payload->>'run_id'
JOIN systems AS s ON s.id = rn.system_id
JOIN allocations AS a ON a.id = s.allocation_id
JOIN resources AS res ON res.id = a.resource_id
LEFT JOIN capture_reap_state AS r ON r.job_id = j.id
WHERE j.kind = %(kind)s
  AND j.state = ANY(%(states)s)
  AND j.updated_at <= now() - %(settle)s
  AND res.kind = ANY(%(provider_kinds)s)
  AND r.reclaimed_at IS NULL
  AND (r.retry_after IS NULL OR r.retry_after <= now())
  AND (
      EXISTS (
          SELECT 1 FROM capture_operations AS o
          WHERE o.job_id = j.id
            AND o.job_attempt = j.attempt
            AND o.state = 'exited'
            AND o.process_absent
            AND o.provider_quiescence <> '{}'::jsonb
            AND o.publication_state IN ('published', 'discarded')
            AND o.spool_disposed_at IS NOT NULL
      )
      OR (
          -- Deliberately NOT qualified by `o.job_attempt = j.attempt`, unlike the branch above.
          -- This asks whether the job ever had a supervised attempt, because that is what decides
          -- whether it belongs to the pre-cutover population at all. Qualifying it by the current
          -- attempt would fail open on a retry: a job whose attempt 1 is still publishing and
          -- whose attempt 2 died before creating its own operation has no row for `j.attempt`, so
          -- a qualified NOT EXISTS is TRUE and the row is dispatched while attempt 1 can still
          -- commit an artifact and still needs its object.
          NOT EXISTS (
              SELECT 1 FROM capture_operations AS o WHERE o.job_id = j.id
          )
          AND EXISTS (
              SELECT 1 FROM capture_operation_cutoff AS c
              WHERE c.singleton AND c.complete AND j.created_at <= c.cutoff_at
          )
      )
  )
ORDER BY (r.job_id IS NOT NULL), r.retry_after, j.updated_at, j.id
LIMIT %(batch)s
"""

# One row records the first outcome for a job and is updated by every later one. `attempts` counts
# provider attempts spent, which is also the backoff exponent. `GREATEST(now(), prior)` plus a
# strictly positive interval advances the deadline past both its previous value and the current
# database time, as ADR-0556 requires — a bare `now() + backoff` would move it *backwards* for a
# row whose prior deadline was further out. The `WHERE` makes a deferral lose to a concurrent
# reclaim rather than writing a state the shape check forbids.
_DEFER_CAPTURE_SQL = """
INSERT INTO capture_reap_state AS s (job_id, attempts, retry_after)
VALUES (%(job_id)s, 1, now() + LEAST(%(base)s::interval, %(cap)s::interval))
ON CONFLICT (job_id) DO UPDATE
SET attempts = s.attempts + 1,
    retry_after = GREATEST(now(), s.retry_after)
        + LEAST(%(base)s::interval * (2 ^ LEAST(s.attempts, %(exponent_cap)s)), %(cap)s::interval)
WHERE s.reclaimed_at IS NULL
"""

_MARK_CAPTURE_RECLAIMED_SQL = """
INSERT INTO capture_reap_state AS s (job_id, attempts, reclaimed_at)
VALUES (%(job_id)s, 1, now())
ON CONFLICT (job_id) DO UPDATE
SET attempts = s.attempts + 1, retry_after = NULL, reclaimed_at = now()
"""


async def reap_orphaned_captures(
    conn: AsyncConnection,
    reapers: Mapping[str, CaptureReaper],
    *,
    settle: timedelta,
    batch: int,
    retry_base: timedelta,
    retry_cap: timedelta,
    budget: timedelta = DEFAULT_LANE_BUDGET,
) -> ReapLaneOutcome:
    """Reclaim traffic-capture host state orphaned by a terminal job row (ADR-0556).

    Nothing outside a ``capture_traffic`` job reclaims its host state: a dead worker can leave an
    attached ``filter-dump`` still writing, a ``failed`` or ``canceled`` row owns a destination
    nobody removes, and reclaim is best-effort on both providers so even a ``succeeded`` row proves
    nothing about its destination. The job row is the durable correlation key, so the sweep
    resolves the System and Resource through the Run and hands one
    :class:`~kdive.providers.infra.reaping.OrphanedCapture` to the reaper registered for that
    Resource kind.

    A kind with no concrete reaper is excluded from selection rather than dispatched and declined,
    so a disabled provider's rows never consume the batch. Both kinds ship disabled here; #1947
    and #1948 each register their own.

    Each candidate's dispatch and its completion write run in **one transaction holding the job's
    ownership fence** — the same fence a capture worker holds as a session lock from before it
    clears prior completion until after detach and reclaim. Process death releases it; a paused or
    partitioned live worker keeps it and refuses this pass. That positive ownership boundary, not
    the settle duration, is what prevents state being created after an absence-tolerant reap.

    ``budget`` caps how long the lane keeps dispatching candidates (ADR-0565). Measured on the
    reconciler process's monotonic clock, per lane per pass, and consulted only **between**
    candidates — never while a provider call is in flight, so it can end no fenced transaction the
    reaper is still mutating host state under. On violation the lane returns after the candidate in
    flight completes, having dispatched fewer than ``batch``; that is not a fault and is not
    counted.
    A candidate the budget never reached writes no deferral, so it keeps its place in the untouched
    leading group and the next pass reaches it first.

    Returns:
        A :class:`ReapLaneOutcome`: ``reaped`` is the number of captures this pass reclaimed.
        Every other outcome is not a fault and is not counted as reaped: a fenced row, a row
        whose ownership chain has no Resource name, a provider that declined, and a provider that
        raised are all deferred behind a database-clock retry deadline with bounded backoff, and
        remain observable in the per-row logs. ``budget_unattempted`` reports how many selected
        candidates the budget stopped the lane from dispatching (#1982); ``0`` when the pass
        dispatched its whole batch.
    """
    require_top_level_transaction(conn, "the orphaned capture sweep")
    kinds = dispatchable_capture_kinds(reapers)
    if not kinds or batch <= 0:
        return ReapLaneOutcome("capture", 0)
    async with conn.transaction():
        candidates = await _orphaned_capture_rows(conn, kinds, settle, batch)
    deadline = _lane_deadline(budget)
    reaped = 0
    for index, row in enumerate(candidates):
        unattempted = _budget_spent(deadline, "capture", remaining=len(candidates) - index)
        if unattempted is not None:
            return ReapLaneOutcome("capture", reaped, unattempted)
        if await _reclaim_capture(
            conn,
            reapers[str(row["provider_kind"])],
            row,
            retry_base=retry_base,
            retry_cap=retry_cap,
        ):
            reaped += 1
    return ReapLaneOutcome("capture", reaped)


async def _orphaned_capture_rows(
    conn: AsyncConnection, kinds: frozenset[str], settle: timedelta, batch: int
) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _ORPHANED_CAPTURE_SQL,
            {
                "kind": _CAPTURE_JOB_KIND_VALUE,
                "states": list(_TERMINAL_CAPTURE_STATE_VALUES),
                "settle": settle,
                "provider_kinds": sorted(kinds),
                "batch": batch,
            },
        )
        return list(await cur.fetchall())


async def _reclaim_capture(
    conn: AsyncConnection,
    reaper: CaptureReaper,
    row: dict[str, Any],
    *,
    retry_base: timedelta,
    retry_cap: timedelta,
) -> bool:
    """Dispatch one candidate under its ownership fence and record the outcome (ADR-0556).

    The provider call sits inside the fenced transaction on purpose: the reaper must hold the fence
    from before it inspects host state through the completion write, or a live owner could create
    state after an absence-tolerant reap already marked the attempt done.

    Every non-reclaim outcome writes a retry deadline, a refused fence included. Skipping one
    without a deadline looks harmless — contention is someone else's live work, not a failure — but
    an unmarked row sorts in the leading untouched group on every pass, so a handful of jobs whose
    owners are paused or partitioned would fill the batch permanently and no eligible row would
    ever be reached. That is exactly the starvation ADR-0556 forbids an ineligible row from
    causing.

    Backoff is the right shape for it rather than an over-punishment, because a fence held on a row
    this sweep can see means a wedged owner, not a busy one: selection only reaches terminal jobs
    past the settle window, and a healthy worker releases the fence when its job leaves ``running``
    — long before then.
    """
    require_top_level_transaction(conn, "the orphaned capture sweep's per-row fence")
    job_id, system_id = row["job_id"], row["system_id"]
    async with conn.transaction():
        if not await try_capture_job_fence(conn, job_id):
            _log.info(
                "reconciler: capture job %s (system %s) is fenced by a live owner; deferring "
                "without a provider call",
                job_id,
                system_id,
            )
            await _defer_capture(conn, job_id, retry_base=retry_base, retry_cap=retry_cap)
            return False
        capture = _orphaned_capture(row)
        if capture is None:
            # ADR-0187 binds a remote reaper to its host by Resource name, and selection does not
            # guess a host. Logged rather than raised, and deferred rather than skipped: skipping
            # would leave an untouched row sorting ahead of every real candidate on every pass.
            _log.warning(
                "reconciler: capture job %s (system %s) names a resource with no name; cannot "
                "bind a reaper, deferring",
                job_id,
                system_id,
            )
            reclaimed = False
        else:
            reclaimed = await _dispatch_capture(reaper, capture, system_id=system_id)
        if not reclaimed:
            await _defer_capture(conn, job_id, retry_base=retry_base, retry_cap=retry_cap)
            return False
        await conn.execute(_MARK_CAPTURE_RECLAIMED_SQL, {"job_id": job_id})
    _log.info(
        "reconciler: reclaimed orphaned capture state for job %s (system %s)", job_id, system_id
    )
    return True


def _orphaned_capture(row: dict[str, Any]) -> OrphanedCapture | None:
    resource_name = row["resource_name"]
    if resource_name is None:
        return None
    return OrphanedCapture(
        provider_kind=str(row["provider_kind"]),
        resource_id=row["resource_id"],
        resource_name=str(resource_name),
        system_id=row["system_id"],
        domain_name=str(row["domain_name"]),
        job_id=row["job_id"],
    )


async def _dispatch_capture(
    reaper: CaptureReaper, capture: OrphanedCapture, *, system_id: UUID
) -> bool:
    """Call one reaper, converting a raise into the same deferral a decline gets.

    Nothing here bounds the call, and nothing here may: this runs inside the fenced transaction, and
    an ``asyncio.timeout`` would cancel the await while the reaper's synchronous libvirt client kept
    running in its worker thread — ending the transaction, and releasing the ownership fence, with
    host state still being mutated. ADR-0556 forbids exactly that: lock release alone is not
    evidence that provider mutation stopped.

    The bound is therefore placed where it can be taken safely (ADR-0565), in two pieces outside
    this call. The reaper's own opener gates each host on a bounded TCP connect, so an unreachable
    host costs ``KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS`` rather than the kernel's ~130 s SYN
    retry budget — which a remote capture reaper inherits only by opening through
    ``remote_libvirt_reaper_connections``, as the :class:`CaptureReaper` port docstring requires.
    And :func:`reap_orphaned_captures` stops dispatching once its pass budget is spent, so a host
    that accepts the connection and then stalls costs the pass one candidate instead of the whole
    batch (#1981 owns bounding that stall itself).
    """
    try:
        return await reaper.reclaim_capture(capture)
    except Exception:  # noqa: BLE001 - one capture's failure must not starve the rest of the pass
        _log.warning(
            "reconciler: reclaiming orphaned capture failed (system %s, job %s); retry after "
            "backoff",
            system_id,
            capture.job_id,
            exc_info=True,
        )
        return False


async def _defer_capture(
    conn: AsyncConnection, job_id: UUID, *, retry_base: timedelta, retry_cap: timedelta
) -> None:
    await conn.execute(
        _DEFER_CAPTURE_SQL,
        {
            "job_id": job_id,
            "base": retry_base,
            "cap": retry_cap,
            "exponent_cap": _BACKOFF_EXPONENT_CAP,
        },
    )
