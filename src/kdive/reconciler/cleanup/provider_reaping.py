"""Provider-owned infrastructure repair for the reconciler."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import (
    LockScope,
    advisory_xact_lock,
    require_top_level_transaction,
    try_advisory_xact_lock,
    try_capture_job_fence,
)
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_STALE_AFTER
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.operations.jobs import JobKind
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import (
    CaptureReaper,
    DumpVolume,
    DumpVolumeReaper,
    InfraReaper,
    OrphanedCapture,
    dispatchable_capture_kinds,
)
from kdive.providers.shared.host_dump_volume_leases import (
    has_live_host_dump_volume_lease,
    reap_stale_host_dump_volume_leases,
)
from kdive.providers.shared.runtime_paths import system_id_from_domain_name
from kdive.reconciler.repairs.allocations import has_active_capture_job
from kdive.reconciler.repairs.systems import gone_system_state_values

_log = logging.getLogger(__name__)

_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES = (JobState.QUEUED.value, JobState.RUNNING.value)
_TEARDOWN_JOB_KIND_VALUE = JobKind.TEARDOWN.value
_TORN_DOWN_SYSTEM_STATE_VALUE = SystemState.TORN_DOWN.value

DEFAULT_DUMP_VOLUME_GRACE = timedelta(minutes=30)

#: How long either host-state reaping lane may keep starting new candidates in one pass (ADR-0565).
#: A third of the reconciler's default 30 s interval, so both lanes spending their whole budget
#: still leaves a third of an interval — minus each lane's in-flight-candidate overshoot — for the
#: rest of the repair catalog. The full limit contract is on
#: :data:`kdive.config.core_settings.RECONCILER_LANE_BUDGET_SECONDS` and repeated on each lane.
DEFAULT_LANE_BUDGET = timedelta(seconds=10)

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


@dataclass(frozen=True, slots=True)
class ReapLaneOutcome:
    """What one host-state reaping-lane pass did (ADR-0565, #1982).

    ``reaped`` keeps the lane's existing meaning — what was reclaimed; a candidate the budget
    left unattempted is not counted. ``budget_unattempted`` is the count of candidates the pass
    would otherwise have attempted but its budget stopped it from starting; ``0`` when the pass
    drained its worklist. Ending short is not a fault and raises nothing — the count is signal,
    surfaced through the pass report and telemetry, never ``kdive.errors``.
    """

    #: Which lane ran — the same name the budget log line uses.
    lane: str
    reaped: int
    budget_unattempted: int = 0


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


def _lane_deadline(budget: timedelta) -> float:
    """The monotonic instant a lane stops starting new candidates (ADR-0565).

    Started by the caller **after** its candidate list is in hand, never at the top of the lane. The
    dump-volume lane's ``list_dump_volumes`` is itself a whole-fleet fan-out costing up to one
    connect timeout per declared host, so a deadline started ahead of it is already spent before the
    loop on a fleet with more down hosts than ``budget / connect_timeout`` — and the lane would then
    attempt zero candidates on every pass, forever. The budget bounds the work a lane chooses to do,
    not the work it must do to know what that work is.
    """
    return time.monotonic() + budget.total_seconds()


def _budget_spent(deadline: float, lane: str, *, remaining: int) -> int | None:
    """How many candidates ``lane`` leaves behind if it must stop starting them now (ADR-0565).

    Returns :data:`None` while the budget lasts, else ``remaining`` — the count of candidates the
    pass would otherwise have attempted. The caller surfaces that count on the lane's
    :class:`ReapLaneOutcome` (#1982); the INFO line stays as the human-readable echo.

    Called only **between** candidates, before the next candidate's transaction opens, so a spent
    budget can never unwind a transaction that is already open. That placement is the whole of the
    ownership guarantee: an ``asyncio.timeout`` around the provider call would instead cancel the
    await while the synchronous libvirt work continued in its worker thread, ending the
    transaction — and releasing the ownership fence or the System lock — with host state still
    being mutated.

    Ending short is not a fault and is not counted as one.
    """
    if time.monotonic() < deadline:
        return None
    _log.info(
        "reconciler: %s lane ended its pass on the budget with %d candidate(s) unattempted; "
        "they are re-derived next pass",
        lane,
        remaining,
    )
    return remaining


async def repair_leaked_domains(conn: AsyncConnection, reaper: InfraReaper) -> int:
    """Destroy provider domains whose owning System is gone and no teardown is in flight.

    The owning System is the domain's metadata tag when present, else the System encoded in
    its ``kdive-<uuid>`` name (ADR-0111): a genuinely orphaned domain that lost its tag but
    matches the naming convention is still ours, and is reaped once no live ``systems`` row
    backs it. A name that does not match the convention is foreign/unmanaged and never
    reaped. The metadata tag stays authoritative when present; the name is a fallback only.
    """
    domains = await reaper.list_owned()
    reaped = 0
    for domain in domains:
        system_id = domain.system_id or system_id_from_domain_name(domain.name)
        if system_id is None:
            continue  # not a kdive System domain → foreign/unmanaged → never reaped
        async with (
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT 1 FROM systems WHERE id = %s AND state <> %s",
                (system_id, _TORN_DOWN_SYSTEM_STATE_VALUE),
            )
            has_live_row = await cur.fetchone() is not None
            await cur.execute(
                "SELECT 1 FROM jobs WHERE state = ANY(%s) "
                "  AND kind = %s AND payload->>'system_id' = %s",
                (
                    list(_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES),
                    _TEARDOWN_JOB_KIND_VALUE,
                    str(system_id),
                ),
            )
            teardown_in_flight = await cur.fetchone() is not None
        if has_live_row or teardown_in_flight:
            continue
        try:
            await reaper.destroy(domain.name)
        except Exception:  # noqa: BLE001 - one domain's failure must not strand the others
            _log.warning(
                "reconciler: destroy of leaked domain %s failed; retry next pass",
                domain.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: leaked domain %s (system %s) reaped", domain.name, system_id)
    return reaped


class ProbeReaper(Protocol):
    """The narrow provider port the reconciler consumes to destroy a leaked probe guest.

    Structurally a subset of :class:`kdive.providers.infra.reaping.InfraReaper` (``destroy(name)``),
    so the reconciler reuses its existing reaper for both the leaked-domain and the
    leaked-probe sweep — a probe guest is destroyed by domain name like any other domain.
    """

    async def destroy(self, name: str) -> None: ...


async def repair_leaked_probe_guests(
    conn: AsyncConnection,
    reaper: ProbeReaper,
    *,
    heartbeat_stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
) -> int:
    """Reap ``guest_egress`` probe guests whose owning doctor run is gone; honor the heartbeat.

    A probe is leaked when its marker row is past its hard TTL **or** its active-run heartbeat
    is stale (the owning ``doctor`` run stopped beating) — and is not already released. A row
    with a **fresh** heartbeat is an in-use probe (a live run) and is **never** reaped (ADR-0091
    §3): the reaper must not destroy a guest mid-check and turn a healthy egress path into a
    spurious ``error``. On a successful destroy the row is stamped ``released_at`` so the
    provider's single-flight slot frees and a re-pass does not re-reap. Per-probe ``destroy``
    failures are isolated (one leak must not strand the others); time predicates run in
    Postgres (never a Python clock).
    """
    rows = await _leaked_probe_rows(conn, heartbeat_stale_after)
    reaped = 0
    for row in rows:
        if not await _destroy_probe(reaper, row):
            continue
        await _mark_probe_released(conn, row["id"])
        reaped += 1
        _log.info("reconciler: leaked egress probe %s reaped", row["domain_name"])
    return reaped


async def _leaked_probe_rows(conn: AsyncConnection, stale_after: timedelta) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, domain_name FROM egress_probe_guests "
            "WHERE released_at IS NULL "
            "  AND (ttl_deadline < now() OR heartbeat_at < now() - %s)",
            (stale_after,),
        )
        return list(await cur.fetchall())


async def _destroy_probe(reaper: ProbeReaper, row: dict) -> bool:
    try:
        await reaper.destroy(row["domain_name"])
    except Exception:  # noqa: BLE001 - one probe's failure must not strand the others
        _log.warning(
            "reconciler: destroy of leaked egress probe %s failed; retry next pass",
            row["domain_name"],
            exc_info=True,
        )
        return False
    return True


async def _mark_probe_released(conn: AsyncConnection, probe_id: UUID) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE egress_probe_guests SET released_at = now() WHERE id = %s", (probe_id,)
        )


async def reap_console_collectors(conn: AsyncConnection, registry: CollectorRegistry) -> int:
    """Finalize and drop console collectors for gone Systems (ADR-0095)."""
    held = registry.system_ids()
    if not held:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM systems WHERE id = ANY(%s)",
            (list(held),),
        )
        states = {row[0]: row[1] for row in await cur.fetchall()}
    reaped = 0
    gone_states = gone_system_state_values()
    for system_id in held:
        state = states.get(system_id)
        if state is not None and state not in gone_states:
            continue
        await registry.finalize_and_drop_async(system_id)
        reaped += 1
        _log.info("reconciler: console collector for gone system %s finalized + reaped", system_id)
    return reaped


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


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
