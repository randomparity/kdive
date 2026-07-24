"""Garbage-collection style reconciler repairs."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import ReclaimInvestigationRootfsPayload
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import DumpVolumeReaper
from kdive.reconciler.repairs.allocations import SYSTEM_RECONCILER_PRINCIPAL, has_active_capture_job
from kdive.reconciler.repairs.systems import gone_system_state_values

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

DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)
DEFAULT_DUMP_VOLUME_GRACE = timedelta(minutes=30)
DEFAULT_REPORT_ARTIFACT_RETENTION = timedelta(days=7)
DEFAULT_INVESTIGATION_CLEANUP_GRACE = timedelta(days=1)
DEFAULT_BUILD_ARTIFACT_RETENTION = timedelta(days=30)
DEFAULT_INVESTIGATION_ROOTFS_RETENTION = timedelta(days=30)

#: Run-owned artifact retention classes the build-artifact sweeps reclaim (ADR-0234 §4, #768): the
#: uploaded combined kernel tar / vmlinux / initrd (``build``) and an internally-built run kernel
#: (``kernel-build``). Deliberately excludes ``build-log`` (run-owned build evidence, ADR-0238) and
#: ``console``/``vmcore`` (system-owned crash evidence). Both sweeps also pin ``owner_kind='runs'``,
#: so operator base-image uploads (system-owned) are out of scope.
_BUILD_RETENTION_CLASSES: tuple[str, ...] = ("build", "kernel-build")


class ArtifactObjectDeleter(Protocol):
    """The object-store delete surface the report-artifact reaper needs."""

    def delete(self, key: str) -> None: ...


async def gc_idempotency_keys(conn: AsyncConnection, retention: timedelta) -> int:
    """Delete ``idempotency_keys`` rows older than ``retention`` (ADR-0040)."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM idempotency_keys WHERE created_at < now() - %s", (retention,)
        )
        deleted = cur.rowcount
    if deleted:
        _log.info("reconciler: GC'd %d idempotency key(s) past retention", deleted)
    return deleted


async def gc_report_artifacts(
    conn: AsyncConnection, store: ArtifactObjectDeleter, retention: timedelta
) -> int:
    """Delete report spreadsheet artifacts (object + row) older than ``retention`` (ADR-0212).

    Scoped strictly to ``owner_kind = 'reports'`` so System-owned evidence is never touched.
    Reports have a synthetic owner with no teardown trigger, so without this sweep their
    objects and rows would accumulate without bound. A per-object store failure is logged and
    retried next pass rather than aborting the sweep.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key FROM artifacts "
            "WHERE owner_kind = 'reports' AND created_at < now() - %s",
            (retention,),
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    deleted = 0
    for artifact_id, object_key in candidates:
        try:
            await asyncio.to_thread(store.delete, object_key)
        except Exception:  # noqa: BLE001 - one object failure must not starve the rest
            _log.warning(
                "reconciler: deleting report artifact object %s failed; retry next pass",
                object_key,
                exc_info=True,
            )
            continue
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        deleted += 1
    if deleted:
        _log.info("reconciler: GC'd %d report artifact(s) past retention", deleted)
    return deleted


async def gc_investigation_artifacts(
    conn: AsyncConnection, store: ArtifactObjectDeleter, grace: timedelta
) -> int:
    """Reclaim run-owned build artifacts of closed investigations past ``grace`` (ADR-0234 §4).

    Deletes object + row for ``owner_kind='runs'`` artifacts whose ``retention_class`` is in
    :data:`_BUILD_RETENTION_CLASSES`, linked via ``runs.investigation_id`` to an investigation whose
    ``cleanup_pending_at`` is older than ``grace``. The marker is cleared once an investigation's
    build artifacts are fully drained, so a reclaimed investigation drops out of the worklist; a
    per-object store failure is logged and retried next pass (leaving the marker set) and never
    aborts the sweep — the deferred, evidence-safe form ADR-0234 constraint (a)/(b) requires.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM investigations "
            "WHERE cleanup_pending_at IS NOT NULL AND cleanup_pending_at < now() - %s",
            (grace,),
        )
        investigation_ids = [row[0] for row in await cur.fetchall()]
    deleted = 0
    for investigation_id in investigation_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT a.id, a.object_key FROM artifacts a JOIN runs r ON r.id = a.owner_id "
                "WHERE a.owner_kind = 'runs' AND a.retention_class = ANY(%s) "
                "AND r.investigation_id = %s",
                (list(_BUILD_RETENTION_CLASSES), investigation_id),
            )
            candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
        drained = True
        for artifact_id, object_key in candidates:
            try:
                await asyncio.to_thread(store.delete, object_key)
            except Exception:  # noqa: BLE001 - one object failure must not starve the rest
                _log.warning(
                    "reconciler: deleting investigation artifact object %s failed; retry next pass",
                    object_key,
                    exc_info=True,
                )
                drained = False
                continue
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
            deleted += 1
        if drained:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE investigations SET cleanup_pending_at = NULL WHERE id = %s",
                    (investigation_id,),
                )
    if deleted:
        _log.info("reconciler: GC'd %d closed-investigation build artifact(s)", deleted)
    return deleted


async def gc_expired_build_artifacts(
    conn: AsyncConnection, store: ArtifactObjectDeleter, retention: timedelta
) -> int:
    """Reclaim run-owned build artifacts older than ``retention`` regardless of close (ADR-0234 §4).

    The TTL backstop for investigations that never close (#768). Same row scope as
    :func:`gc_investigation_artifacts` (``owner_kind='runs'`` and a build ``retention_class``) but
    gated on ``artifacts.created_at`` rather than the close marker. A per-object store failure is
    logged and retried next pass rather than aborting the sweep (like :func:`gc_report_artifacts`).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key FROM artifacts "
            "WHERE owner_kind = 'runs' AND retention_class = ANY(%s) AND created_at < now() - %s",
            (list(_BUILD_RETENTION_CLASSES), retention),
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    deleted = 0
    for artifact_id, object_key in candidates:
        try:
            await asyncio.to_thread(store.delete, object_key)
        except Exception:  # noqa: BLE001 - one object failure must not starve the rest
            _log.warning(
                "reconciler: deleting expired build artifact object %s failed; retry next pass",
                object_key,
                exc_info=True,
            )
            continue
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        deleted += 1
    if deleted:
        _log.info("reconciler: GC'd %d build artifact(s) past TTL", deleted)
    return deleted


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
    conn: AsyncConnection, reaper: DumpVolumeReaper, grace: timedelta
) -> int:
    """Delete host_dump volumes orphaned by a non-graceful worker/host crash (ADR-0094)."""
    volumes = await reaper.list_dump_volumes()
    if not volumes:
        return 0
    cutoff_epoch = await _now_epoch(conn) - grace.total_seconds()
    reaped = 0
    for volume in volumes:
        if volume.mtime_epoch_s >= cutoff_epoch:
            continue
        if volume.system_id is not None and await has_active_capture_job(conn, volume.system_id):
            continue
        try:
            await reaper.delete_dump_volume(volume.name)
        except Exception:  # noqa: BLE001 - one volume failure must not starve the rest
            _log.warning(
                "reconciler: deleting orphaned dump volume %s failed; retry next pass",
                volume.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: reaped orphaned host_dump volume %s", volume.name)
    return reaped


async def sweep_investigation_rootfs_reclaim(conn: AsyncConnection, grace: timedelta) -> int:
    """Enqueue a rootfs reclaim job per closed investigation past ``grace`` (ADR-0442 §1, #1522).

    DB-only: selects investigations by the **dedicated** ``rootfs_cleanup_pending_at`` marker (never
    the build sweep's ``cleanup_pending_at``, so a drained build artifact cannot starve this), reads
    their committed ``owner_kind='investigations'``/``retention_class='rootfs'`` rows, and hands the
    worklist to the worker. It touches neither the host filesystem nor the object store — the whole
    reclaim, including the liveness gate, runs on the worker that created the staging tree.

    A marker past grace with **no** rootfs rows left is cleared here rather than enqueued: there is
    nothing for a worker to reclaim, so the investigation would otherwise stay on the worklist
    forever. Returns the number of reclaim jobs ensured this pass.
    """
    async with conn.cursor() as cur:
        await cur.execute(_CLOSE_DRIVEN_INV_SQL, (grace,))
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    enqueued = 0
    for investigation_id, project in candidates:
        artifact_ids = await _investigation_rootfs_artifact_ids(conn, investigation_id)
        if not artifact_ids:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE investigations SET rootfs_cleanup_pending_at = NULL WHERE id = %s",
                    (investigation_id,),
                )
            continue
        await _enqueue_rootfs_reclaim(conn, investigation_id, project, artifact_ids)
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
    for investigation_id, (project, artifact_ids) in due.items():
        await _enqueue_rootfs_reclaim(conn, investigation_id, project, artifact_ids)
    if due:
        _log.info("reconciler: enqueued %d past-TTL rootfs reclaim job(s)", len(due))
    return len(due)


async def _investigation_rootfs_artifact_ids(
    conn: AsyncConnection, investigation_id: UUID
) -> list[UUID]:
    """Every committed uploaded-rootfs artifact id of ``investigation_id``."""
    async with conn.cursor() as cur:
        await cur.execute(_INV_ROOTFS_OBJECTS_SQL, (investigation_id,))
        return [row[0] for row in await cur.fetchall()]


async def _enqueue_rootfs_reclaim(
    conn: AsyncConnection, investigation_id: UUID, project: str, artifact_ids: list[UUID]
) -> None:
    """Ensure the one reclaim job for ``investigation_id``, recycling a terminal attempt.

    The dedup key is **stable** per investigation (ADR-0442 §6), so the sweeps hold exactly one job
    row per investigation forever instead of one per ~30 s pass. A ``queued``/``running`` job is
    left untouched (in-flight dedup with no separate pre-check); a ``succeeded``/``failed`` one is
    reset to a fresh attempt carrying this pass's due set. ``recycle_canceled`` is on because the
    slot is reconciler-owned: a canceled job wedged in it would silently disable reclaim for the
    investigation forever, which is the failure mode #1522 exists to remove.
    """
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
        f"rootfs-reclaim:{investigation_id}",
        recycle_terminal=True,
        recycle_canceled=True,
    )


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
