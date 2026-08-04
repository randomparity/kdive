"""Investigation rootfs cleanup repairs for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.capacity.state import ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import ReclaimInvestigationRootfsPayload
from kdive.reconciler.repairs.allocations import SYSTEM_RECONCILER_PRINCIPAL

_log = logging.getLogger(__name__)

_CLOSE_DRIVEN_INV_SQL = (
    "SELECT id, project FROM investigations "
    "WHERE rootfs_cleanup_pending_at IS NOT NULL AND rootfs_cleanup_pending_at < now() - %s"
)
_INV_ROOTFS_OBJECTS_SQL = (
    "SELECT id FROM artifacts "
    "WHERE owner_kind = 'investigations' AND retention_class = 'rootfs' AND owner_id = %s"
)
#: TTL backstop scope: committed investigation-rootfs objects past retention on a **never-closed**
#: investigation (``open``/``active`` — a closed one is the close-driven sweep's job, keyed on the
#: since-cleared marker; ADR-0441 §6). The state predicate also keeps this worklist disjoint from
#: the close-driven one, so the two never contend for the shared per-investigation dedup key.
_TTL_ROOTFS_OBJECTS_SQL = (
    "SELECT a.id, a.owner_id, i.project FROM artifacts a "
    "JOIN investigations i ON i.id = a.owner_id "
    "WHERE a.owner_kind = 'investigations' AND a.retention_class = 'rootfs' "
    "AND a.created_at < now() - %s AND i.state IN ('open', 'active')"
)
#: The :class:`~kdive.domain.capacity.state.SystemState`\ s in which a System legitimately needs its
#: rootfs base with no overlay file yet — read from ADR-0441 §6's curated set rather than restated,
#: so this lane's anti-join and the reclaim's own pin gate cannot drift on what "mid-materialize"
#: means, and a new non-terminal state added without being classified reddens that set's
#: exhaustiveness guard instead of silently escaping here. Sorted so the parameter is deterministic.
_MID_MATERIALIZE_STATE_VALUES: tuple[str, ...] = tuple(
    sorted(state.value for state in ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES)
)
#: Staging-drain backstop scope (ADR-0494 §2, #1559): a **never-closed** investigation
#: (``open``/``active``) that once provisioned a System off an uploaded rootfs and whose rootfs
#: ``artifacts`` rows have since **all** drained. Keyed on ``systems`` rather than on ``artifacts``
#: precisely because there is no row left to key on — a base orphaned in that state (published
#: after its own reclaim, or left by a worker killed between the publish and the row commit) is
#: reached by neither :data:`_CLOSE_DRIVEN_INV_SQL`, whose marker only ``investigations.close``
#: sets, nor :data:`_TTL_ROOTFS_OBJECTS_SQL`, which is a pure ``artifacts`` join.
#:
#: The ``systems`` row is the *causal* record: a base is only ever staged for a System whose
#: profile names an ``upload`` rootfs, and Systems are retired in place (``torn_down``) rather than
#: deleted, so the trigger outlives every row the base itself had. The ``NOT EXISTS`` keeps this
#: worklist disjoint from the TTL lane's, and the ``open``/``active`` predicate keeps it disjoint
#: from the close-driven one, so the three never contend for the shared per-investigation dedup key.
#:
#: The age gate is ``investigations.created_at``, **not** the ``systems`` row's (ADR-0501, #1686).
#: Content-addressed reuse (ADR-0441) lets a System minutes old attach to a checksum this
#: investigation staged months ago, and a System-keyed gate then withheld the drained half's only
#: retry until that System itself aged past retention — up to 30 days against a lane whose intended
#: cadence is :data:`ROOTFS_STAGING_DRAIN_BACKOFF`. The investigation is what outlives every base it
#: staged and what the same retention policy already governs, so it is what the gate reads.
#:
#: The second ``NOT EXISTS`` carries, explicitly, the one protection the discarded System-age gate
#: was proxying for: a System between its staging ``mkdir`` and its ``artifacts``-row resolution,
#: which the drain tail's ``rmdir`` would otherwise fail out from under (ADR-0494's own rejected
#: "run the ``rmdir`` unconditionally"). It is **investigation**-scoped rather than per-row because
#: the job carries an empty worklist and the tail sweeps the one staging directory every System of
#: the investigation shares — so a settled sibling must not re-admit it. An age proxy could not
#: express that anyway: on ``main`` a past-retention sibling already admitted the job while another
#: System provisioned.
_UNOWNED_STAGING_INV_SQL = (
    "SELECT DISTINCT s.investigation_id, i.project FROM systems s "
    "JOIN investigations i ON i.id = s.investigation_id "
    "WHERE i.state IN ('open', 'active') AND i.created_at < now() - %s "
    "AND s.provisioning_profile #>> '{provider,local-libvirt,rootfs,kind}' = 'upload' "
    "AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.owner_kind = 'investigations' "
    "AND a.retention_class = 'rootfs' AND a.owner_id = s.investigation_id) "
    "AND NOT EXISTS (SELECT 1 FROM systems m WHERE m.investigation_id = s.investigation_id "
    "AND m.state = ANY(%s))"
)

DEFAULT_INVESTIGATION_ROOTFS_RETENTION = timedelta(days=30)

#: How long a settled rootfs reclaim job holds its per-investigation slot before the sweeps re-issue
#: it (ADR-0442 §6). The sweeps run every ~30 s but reclaim is grace/TTL-governed in days, so a
#: faulting reclaim retrying every few minutes converges just as fast while keeping the failed row
#: inspectable and keeping a permission wall from becoming a retry storm against the object store.
ROOTFS_RECLAIM_RETRY_BACKOFF = timedelta(minutes=5)

#: How long the staging-drain lane's job holds the same slot (ADR-0494 §5). Deliberately much longer
#: than :data:`ROOTFS_RECLAIM_RETRY_BACKOFF`, because that lane's worklist is a **steady state**
#: rather than a condition that clears: a ``systems`` row is retired in place and never leaves the
#: match, so every never-closed investigation that ever staged an uploaded base is selected on every
#: pass for the rest of its life, whether or not its staging directory holds anything. At the shared
#: 5-minute backoff that is ~288 jobs a day per such investigation, permanently. The bytes this lane
#: reclaims are already governed in *days* by ``investigation_rootfs_retention``, so a six-hourly
#: sweep converges just as fast against the leak it exists for while dropping the steady-state cost
#: by ~72x. The other two lanes keep the short backoff: their worklists drain, so their churn ends.
ROOTFS_STAGING_DRAIN_BACKOFF = timedelta(hours=6)


async def sweep_investigation_rootfs_reclaim(conn: AsyncConnection, grace: timedelta) -> int:
    """Enqueue a rootfs reclaim job per closed investigation past ``grace`` (ADR-0442 §1, #1522).

    DB-only: selects investigations by the **dedicated** ``rootfs_cleanup_pending_at`` marker (never
    the build sweep's ``cleanup_pending_at``, so a drained build artifact cannot starve this), reads
    their committed ``owner_kind='investigations'``/``retention_class='rootfs'`` rows, and hands the
    worklist to the worker. It touches neither the host filesystem nor the object store — the whole
    reclaim, including the liveness gate, runs on the worker that created the staging tree.

    A marker past grace with **no** rootfs rows left still gets a job, carrying an empty worklist:
    the handler falls straight through to its drain tail, which sweeps the staging directory (a
    crash-orphaned SENSITIVE ``*.partial`` no row owns) and clears the marker. Short-circuiting that
    here would either strand the orphan or put a filesystem write back in the reconciler, and it
    would split one drain rule into two. Returns the number of reclaim jobs ensured this pass.
    """
    async with conn.cursor() as cur:
        await cur.execute(_CLOSE_DRIVEN_INV_SQL, (grace,))
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    enqueued = 0
    for investigation_id, project in candidates:
        artifact_ids = await _investigation_rootfs_artifact_ids(conn, investigation_id)
        if await _try_enqueue_rootfs_reclaim(conn, investigation_id, project, artifact_ids):
            enqueued += 1
    if enqueued:
        _log.info("reconciler: enqueued %d closed-investigation rootfs reclaim job(s)", enqueued)
    return enqueued


async def sweep_expired_investigation_rootfs_reclaim(
    conn: AsyncConnection, retention: timedelta
) -> int:
    """Enqueue a reclaim job per never-closed investigation past ``retention`` (TTL backstop).

    The mandatory backstop (ADR-0441 §6): a never-closed investigation would otherwise accumulate
    SENSITIVE bases forever. Gated on ``artifacts.created_at`` and scoped to ``open``/``active``
    investigations, so its worklist is disjoint from the close-driven sweep's (a closed
    investigation is in neither state) and the two never contend for the shared per-investigation
    dedup key. Only the past-retention rows are handed over, keeping the TTL policy here rather than
    duplicating it into the worker. Returns the number of reclaim jobs ensured this pass.
    """
    async with conn.cursor() as cur:
        await cur.execute(_TTL_ROOTFS_OBJECTS_SQL, (retention,))
        rows = await cur.fetchall()
    due: dict[UUID, tuple[str, list[UUID]]] = {}
    for artifact_id, investigation_id, project in rows:
        _project, ids = due.setdefault(investigation_id, (str(project), []))
        ids.append(artifact_id)
    enqueued = 0
    for investigation_id, (project, artifact_ids) in due.items():
        if await _try_enqueue_rootfs_reclaim(conn, investigation_id, project, artifact_ids):
            enqueued += 1
    if enqueued:
        _log.info("reconciler: enqueued %d past-TTL rootfs reclaim job(s)", enqueued)
    return enqueued


async def sweep_unowned_investigation_rootfs_staging(
    conn: AsyncConnection, retention: timedelta
) -> int:
    """Enqueue a staging-drain job per never-closed investigation whose rootfs rows all drained.

    The filesystem-drain backstop (ADR-0494 §2, #1559). Both existing lanes are anchored on state a
    leaked base does not have: the close-driven lane needs ``rootfs_cleanup_pending_at``, which only
    ``investigations.close`` sets, and the TTL lane is a pure ``artifacts`` join that selects
    nothing once the rows are gone. So a base orphaned in a never-closed investigation whose rows
    have drained is reclaimed by nothing at all until a human closes it.

    This lane's worklist is :data:`_UNOWNED_STAGING_INV_SQL` — the ``systems`` rows that reference
    an uploaded base — and the job it issues carries an **empty** ``artifact_ids``, so the handler
    falls straight through its reclaim loop to the drain tail that sweeps the staging directory.
    That is the same empty-worklist path :func:`sweep_investigation_rootfs_reclaim` already relies
    on for a marker past grace with no rows left, so no new handler behaviour is introduced here.

    A job is issued for every matching investigation, including ones whose staging directory is
    already empty — which the reconciler cannot see (it holds no filesystem, ADR-0442). That is a
    **steady state**, not a condition that clears: a ``systems`` row is retired in place and never
    leaves the match, so a never-closed investigation stays selected for the rest of its life. It is
    therefore gated on :data:`ROOTFS_STAGING_DRAIN_BACKOFF` rather than on the neighbouring lanes'
    5-minute slot, which caps the permanent cost at four passes a day per such investigation. A
    DB-side "is it empty" answer would need durable per-investigation state whose only reader is
    this sweep. Returns the number of jobs ensured this pass.

    ``retention`` is applied to ``investigations.created_at``, and a System in one of
    :data:`_MID_MATERIALIZE_STATE_VALUES` excludes its whole investigation for as long as it sits
    there (ADR-0501, #1686). Both are properties of :data:`_UNOWNED_STAGING_INV_SQL`; the reasoning
    is there.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            _UNOWNED_STAGING_INV_SQL, (retention, list(_MID_MATERIALIZE_STATE_VALUES))
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    enqueued = 0
    for investigation_id, project in candidates:
        if await _try_enqueue_rootfs_reclaim(
            conn, investigation_id, project, [], backoff=ROOTFS_STAGING_DRAIN_BACKOFF
        ):
            enqueued += 1
    if enqueued:
        _log.info("reconciler: enqueued %d unowned-rootfs-staging drain job(s)", enqueued)
    return enqueued


async def _investigation_rootfs_artifact_ids(
    conn: AsyncConnection, investigation_id: UUID
) -> list[UUID]:
    """Every committed uploaded-rootfs artifact id of ``investigation_id``."""
    async with conn.cursor() as cur:
        await cur.execute(_INV_ROOTFS_OBJECTS_SQL, (investigation_id,))
        return [row[0] for row in await cur.fetchall()]


async def _try_enqueue_rootfs_reclaim(
    conn: AsyncConnection,
    investigation_id: UUID,
    project: str,
    artifact_ids: list[UUID],
    *,
    backoff: timedelta | None = None,
) -> bool:
    """Issue one investigation's reclaim job, logging and skipping a fault rather than aborting.

    Matches the neighbouring sweeps' "one failure must not starve the rest" contract: a fault on one
    investigation leaves its worklist untouched (the marker stays set, the rows stay) and the pass
    continues with the next.
    """
    try:
        # Resolved here rather than as a default argument: a default is bound once at import, so
        # a test monkeypatching the module attribute would silently keep the imported value.
        return await _enqueue_rootfs_reclaim(
            conn,
            investigation_id,
            project,
            artifact_ids,
            backoff=ROOTFS_RECLAIM_RETRY_BACKOFF if backoff is None else backoff,
        )
    except Exception:  # noqa: BLE001 - one enqueue failure must not starve the rest
        _log.warning(
            "reconciler: enqueuing the rootfs reclaim for investigation %s failed; retry next pass",
            investigation_id,
            exc_info=True,
        )
        return False


async def _enqueue_rootfs_reclaim(
    conn: AsyncConnection,
    investigation_id: UUID,
    project: str,
    artifact_ids: list[UUID],
    *,
    backoff: timedelta,
) -> bool:
    """Issue the one reclaim job for ``investigation_id``; return whether one was admitted.

    The dedup key is **stable** per investigation (ADR-0442 §6), so the sweeps hold at most one job
    row per investigation instead of one per ~30 s pass. Admission is gated here rather than left to
    ``queue``'s ``recycle_terminal`` for the one reason the sweep cadence still makes load-bearing:
    a settled job is left alone until :data:`ROOTFS_RECLAIM_RETRY_BACKOFF` has passed, so a reclaim
    that keeps faulting retries on the order of minutes rather than twice a minute — and its
    ``failed`` row stays inspectable for that window instead of being reset within 30 s.

    ADR-0442 §6 also cited dispatch fairness — a recycle left ``created_at`` at the original
    creation, so a repeatedly-recycled background job sorted ahead of every job enqueued after it.
    ADR-0447 fixed that in the primitive (the recycle now re-dates ``created_at`` too), so this
    delete-and-insert no longer carries that burden; the backoff is what keeps it here.

    The delete and the insert share one transaction, so a fault between them cannot leave the
    investigation with neither a failure record nor a queued reclaim. A ``queued``/``running`` job
    is left untouched (in-flight dedup). ``max_attempts=1`` because an in-job retry of a permission
    wall or a dead store buys nothing the next pass does not: the sweep is the retry loop.
    """
    dedup_key = f"rootfs-reclaim:{investigation_id}"
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT state FROM jobs WHERE dedup_key = %s "
                "AND (state NOT IN ('succeeded', 'failed', 'canceled') "
                "     OR updated_at > now() - %s) FOR UPDATE",
                (dedup_key, backoff),
            )
            if await cur.fetchone() is not None:
                return False
            # The predicate rides the DELETE too, not just the fast-path SELECT above: a SELECT
            # that matches nothing locks nothing, so two concurrent passes could both reach here —
            # and an unconditional delete would then drop the job the other just admitted.
            await cur.execute(
                "DELETE FROM jobs WHERE dedup_key = %s "
                "AND state IN ('succeeded', 'failed', 'canceled') AND updated_at <= now() - %s",
                (dedup_key, backoff),
            )
        await queue.enqueue(
            conn,
            JobKind.RECLAIM_INVESTIGATION_ROOTFS,
            ReclaimInvestigationRootfsPayload(
                investigation_id=str(investigation_id),
                artifact_ids=[str(a) for a in artifact_ids],
            ),
            {
                "principal": SYSTEM_RECONCILER_PRINCIPAL,
                "agent_session": None,
                "project": project,
            },
            dedup_key,
            max_attempts=1,
        )
    return True
