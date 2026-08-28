"""Artifact-retention repairs for the reconciler."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction
from kdive.reconciler.repairs.systems import gone_system_state_values

_log = logging.getLogger(__name__)

DEFAULT_REPORT_ARTIFACT_RETENTION = timedelta(days=7)
DEFAULT_INVESTIGATION_CLEANUP_GRACE = timedelta(days=1)
DEFAULT_BUILD_ARTIFACT_RETENTION = timedelta(days=30)

#: Run-owned artifact retention classes the build-artifact sweeps reclaim (ADR-0234 §4, #768): the
#: uploaded combined kernel tar / vmlinux / initrd (``build``) and an internally-built run kernel
#: (``kernel-build``). Deliberately excludes ``build-log`` (run-owned build evidence, ADR-0238) and
#: ``console``/``vmcore`` (system-owned crash evidence). Both sweeps also pin ``owner_kind='runs'``,
#: so operator base-image uploads (system-owned) are out of scope.
_BUILD_RETENTION_CLASSES: tuple[str, ...] = ("build", "kernel-build")
_SYSTEM_TEARDOWN_ARTIFACT_PATTERNS: tuple[str, ...] = (
    "%console-part-%",
    "%sysrq-diagnostic-%",
)
_SYSTEM_ARTIFACT_KEYS_PER_PASS = 10
_SYSTEM_ARTIFACT_CURSOR_LANE = "row-backed"
_BUILD_GENERATIONS_PER_PASS = 50
_BUILD_GENERATION_SCAN_PER_PASS = 200
_MAX_BUILD_GENERATION_PASS_ROWS = 1_000
BUILD_GENERATION_RETRY_BACKOFF = timedelta(minutes=5)

_UNPINNED_GENERATION_SQL = (
    "((ib.state = 'reclaiming' OR (ib.state = 'active' "
    "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.investigation_id = ib.investigation_id "
    "AND r.build_ref = ib.build_ref AND r.state IN ('created', 'running')) "
    "AND NOT EXISTS (SELECT 1 FROM jobs j JOIN runs r "
    "ON r.id::text = j.payload->>'run_id' "
    "WHERE r.investigation_id = ib.investigation_id AND r.build_ref = ib.build_ref "
    "AND j.kind = 'install' AND j.state IN ('queued', 'running')))) "
    "AND NOT EXISTS (SELECT 1 FROM investigation_build_uses u "
    "WHERE u.investigation_id = ib.investigation_id AND u.generation = ib.generation))"
)


class ArtifactObjectDeleter(Protocol):
    """The bounded retired-key delete surface the artifact reapers need."""

    def delete_retired_key_batch(self, key: str, limit: int) -> bool: ...


class ExactArtifactObjectDeleter(Protocol):
    """The exact-version delete surface required by generation reclamation."""

    def delete_version(self, key: str, version_id: str) -> None: ...


async def _retire_artifact_candidates(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    candidates: list[tuple[UUID, str]],
    operation: str,
) -> tuple[int, bool]:
    """Retire each object key and delete rows whose bounded retirement completes."""
    deleted = 0
    drained = True
    for artifact_id, object_key in candidates:
        try:
            complete = await asyncio.to_thread(store.delete_retired_key_batch, object_key, 20)
        except Exception:  # noqa: BLE001 - one object failure must not starve sibling rows
            _log.warning(
                "reconciler: deleting %s artifact object %s failed; retry next pass",
                operation,
                object_key,
                exc_info=True,
            )
            drained = False
            continue
        if not complete:
            _log.info(
                "reconciler: %s artifact object %s has more retired versions; retry next pass",
                operation,
                object_key,
            )
            drained = False
            continue
        async with conn.transaction():
            await conn.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        deleted += 1
    return deleted, drained


async def _mark_generation_reclaiming(
    conn: AsyncConnection, investigation_id: UUID, generation: UUID
) -> dict[str, dict[str, str]] | None:
    """Fence one generation after rechecking its Run, install-job, and use pins."""
    require_top_level_transaction(conn, "mark Investigation build generation reclaiming")
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        row = await (
            await conn.execute(
                "SELECT state, build_ref, artifacts FROM investigation_builds "
                "WHERE investigation_id = %s AND generation = %s FOR UPDATE",
                (investigation_id, generation),
            )
        ).fetchone()
        if row is None:
            return None
        state, build_ref, artifacts = row
        use = await (
            await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM investigation_build_uses "
                "WHERE investigation_id = %s AND generation = %s)",
                (investigation_id, generation),
            )
        ).fetchone()
        if use is None or use[0]:
            return None
        if state == "active":
            pin = await (
                await conn.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM runs r WHERE r.investigation_id = %s "
                    "AND r.build_ref = %s AND r.state IN ('created', 'running') "
                    "UNION ALL SELECT 1 FROM jobs j JOIN runs r "
                    "ON r.id::text = j.payload->>'run_id' "
                    "WHERE r.investigation_id = %s AND r.build_ref = %s "
                    "AND j.kind = 'install' AND j.state IN ('queued', 'running'))",
                    (
                        investigation_id,
                        build_ref,
                        investigation_id,
                        build_ref,
                    ),
                )
            ).fetchone()
            if pin is None or pin[0]:
                return None
            await conn.execute(
                "UPDATE investigation_builds SET state = 'reclaiming' "
                "WHERE investigation_id = %s AND generation = %s AND state = 'active'",
                (investigation_id, generation),
            )
        return artifacts


async def _reclaim_generation(
    conn: AsyncConnection,
    store: ExactArtifactObjectDeleter,
    investigation_id: UUID,
    generation: UUID,
) -> int:
    require_top_level_transaction(conn, "reclaim Investigation build generation")
    artifacts = await _mark_generation_reclaiming(conn, investigation_id, generation)
    if artifacts is None:
        return 0
    for artifact in artifacts.values():
        try:
            await asyncio.to_thread(
                store.delete_version, str(artifact["key"]), str(artifact["version_id"])
            )
        except Exception:  # noqa: BLE001 - preserve reclaiming state for a later pass
            async with (
                conn.transaction(),
                advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
            ):
                await conn.execute(
                    "UPDATE investigation_builds SET reclaim_retry_at = now() + %s "
                    "WHERE investigation_id = %s AND generation = %s",
                    (BUILD_GENERATION_RETRY_BACKOFF, investigation_id, generation),
                )
            _log.warning(
                "reconciler: deleting Investigation build generation %s failed; retry next pass",
                generation,
                exc_info=True,
            )
            return 0
    keys = [str(artifact["key"]) for artifact in artifacts.values()]
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        await conn.execute(
            "INSERT INTO investigation_build_tombstones "
            "(investigation_id, build_ref, expires_at) "
            "SELECT investigation_id, build_ref, expires_at FROM investigation_builds "
            "WHERE investigation_id = %s AND generation = %s AND state = 'reclaiming' "
            "ON CONFLICT (investigation_id, build_ref) DO NOTHING",
            (investigation_id, generation),
        )
        result = await conn.execute(
            "DELETE FROM investigation_builds WHERE investigation_id = %s "
            "AND generation = %s AND state = 'reclaiming' RETURNING generation",
            (investigation_id, generation),
        )
        if await result.fetchone() is None:
            return 0
        await conn.execute(
            "DELETE FROM artifacts WHERE owner_kind = 'investigations' AND owner_id = %s "
            "AND object_key = ANY(%s)",
            (investigation_id, keys),
        )
    return len(keys)


async def _direct_generation_candidates(
    conn: AsyncConnection, investigation_id: UUID, limit: int
) -> list[tuple[UUID, UUID]]:
    rows = await (
        await conn.execute(
            "SELECT ib.investigation_id, ib.generation FROM investigation_builds ib "
            "WHERE ib.investigation_id = %s AND " + _UNPINNED_GENERATION_SQL + " "
            "AND (ib.reclaim_retry_at IS NULL OR ib.reclaim_retry_at <= now()) "
            "ORDER BY ib.generation LIMIT %s",
            (investigation_id, limit),
        )
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


async def _background_generation_candidates(
    conn: AsyncConnection,
    *,
    lane: str,
    expired: bool,
    closed_grace: timedelta | None,
    limit: int,
    scan_limit: int,
) -> tuple[list[tuple[UUID, UUID]], tuple[UUID, UUID] | None]:
    cursor = await (
        await conn.execute(
            "SELECT investigation_id, generation FROM investigation_build_gc_cursor "
            "WHERE lane = %s FOR UPDATE",
            (lane,),
        )
    ).fetchone()
    after = cursor if cursor is not None and cursor[0] is not None else None
    if after is None:
        scanned = await (
            await conn.execute(
                "SELECT investigation_id, generation FROM investigation_builds "
                "ORDER BY investigation_id, generation LIMIT %s",
                (scan_limit,),
            )
        ).fetchall()
    else:
        scanned = await (
            await conn.execute(
                "SELECT investigation_id, generation FROM investigation_builds "
                "WHERE (investigation_id, generation) > (%s, %s) "
                "ORDER BY investigation_id, generation LIMIT %s",
                (*after, scan_limit),
            )
        ).fetchall()
        remaining = scan_limit - len(scanned)
        if remaining:
            scanned += await (
                await conn.execute(
                    "SELECT investigation_id, generation FROM investigation_builds "
                    "WHERE (investigation_id, generation) <= (%s, %s) "
                    "ORDER BY investigation_id, generation LIMIT %s",
                    (*after, remaining),
                )
            ).fetchall()
    if not scanned:
        return [], None
    eligibility = (
        "(ib.state = 'reclaiming' OR ib.expires_at <= now())"
        if expired
        else "(ib.state = 'reclaiming' OR (i.cleanup_pending_at IS NOT NULL "
        "AND i.cleanup_pending_at < now() - %s))"
    )
    join = "" if expired else " JOIN investigations i ON i.id = ib.investigation_id"
    eligibility_params: tuple[object, ...] = () if expired else (closed_grace,)
    rows = await (
        await conn.execute(
            "WITH scanned AS (SELECT * FROM unnest(%s::uuid[], %s::uuid[]) "
            "AS s(investigation_id, generation)), eligible AS ("
            "SELECT ib.investigation_id, ib.generation, row_number() OVER "
            "(PARTITION BY ib.investigation_id ORDER BY ib.generation) AS tenant_rank "
            "FROM scanned s JOIN investigation_builds ib USING (investigation_id, generation)"
            + join
            + " WHERE "
            + eligibility
            + " AND "
            + _UNPINNED_GENERATION_SQL
            + " AND (ib.reclaim_retry_at IS NULL OR ib.reclaim_retry_at <= now())) "
            "SELECT investigation_id, generation FROM eligible "
            "ORDER BY tenant_rank, investigation_id, generation LIMIT %s",
            (
                [row[0] for row in scanned],
                [row[1] for row in scanned],
                *eligibility_params,
                limit,
            ),
        )
    ).fetchall()
    return [(row[0], row[1]) for row in rows], (scanned[-1][0], scanned[-1][1])


async def _generation_candidates(
    conn: AsyncConnection,
    *,
    investigation_id: UUID | None = None,
    expired: bool = False,
    closed_grace: timedelta | None = None,
    limit: int = _BUILD_GENERATIONS_PER_PASS,
) -> list[tuple[UUID, UUID]]:
    require_top_level_transaction(conn, "select Investigation build reclaim candidates")
    capped_limit = min(limit, _MAX_BUILD_GENERATION_PASS_ROWS)
    capped_scan_limit = min(_BUILD_GENERATION_SCAN_PER_PASS, _MAX_BUILD_GENERATION_PASS_ROWS)
    async with conn.transaction():
        if investigation_id is not None:
            return await _direct_generation_candidates(conn, investigation_id, capped_limit)
        elif expired or closed_grace is not None:
            lane = "expired" if expired else "closed"
            rows, last_scanned = await _background_generation_candidates(
                conn,
                lane=lane,
                expired=expired,
                closed_grace=closed_grace,
                limit=capped_limit,
                scan_limit=capped_scan_limit,
            )
            if last_scanned is not None:
                await conn.execute(
                    "UPDATE investigation_build_gc_cursor SET investigation_id = %s, "
                    "generation = %s "
                    "WHERE lane = %s",
                    (*last_scanned, lane),
                )
        else:
            rows = await (
                await conn.execute(
                    "SELECT ib.investigation_id, ib.generation FROM investigation_builds ib WHERE "
                    + _UNPINNED_GENERATION_SQL
                    + " AND (ib.reclaim_retry_at IS NULL OR ib.reclaim_retry_at <= now())"
                    + " ORDER BY ib.investigation_id, ib.generation LIMIT %s",
                    (capped_limit,),
                )
            ).fetchall()
    return [(row[0], row[1]) for row in rows]


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
    deleted, _ = await _retire_artifact_candidates(conn, store, candidates, "report")
    if deleted:
        _log.info("reconciler: GC'd %d report artifact(s) past retention", deleted)
    return deleted


async def gc_system_artifacts(conn: AsyncConnection, store: ArtifactObjectDeleter) -> int:
    """Finish bounded retirement for artifact rows retained on gone Systems.

    Teardown gives each console-part and diagnostic SysRq key one bounded attempt. An incomplete
    history or store fault retains its row, and this recurring repair retries one bounded batch per
    row on every pass. The row is removed only after the store reports the retired key complete.
    """
    async with conn.transaction():
        observed = await _read_system_artifact_cursor(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT a.id, a.object_key FROM artifacts a JOIN systems s ON s.id = a.owner_id "
            "WHERE a.owner_kind = 'systems' AND s.state = ANY(%s) "
            "AND a.object_key LIKE ANY(%s) "
            "ORDER BY CASE WHEN %s::uuid IS NOT NULL AND a.id <= %s::uuid THEN 1 ELSE 0 END, "
            "a.id LIMIT %s",
            (
                list(gone_system_state_values()),
                list(_SYSTEM_TEARDOWN_ARTIFACT_PATTERNS),
                observed,
                observed,
                _SYSTEM_ARTIFACT_KEYS_PER_PASS,
            ),
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    deleted, _ = await _retire_artifact_candidates(conn, store, candidates, "gone-System")
    if deleted:
        _log.info("reconciler: GC'd %d gone-System artifact(s)", deleted)
    next_cursor = str(candidates[-1][0]) if candidates else None
    async with conn.transaction():
        await conn.execute(
            "UPDATE system_object_sweep_cursors SET after_key = %s, updated_at = now() "
            "WHERE lane = %s AND after_key IS NOT DISTINCT FROM %s",
            (next_cursor, _SYSTEM_ARTIFACT_CURSOR_LANE, observed),
        )
    return deleted


async def _read_system_artifact_cursor(conn: AsyncConnection) -> str | None:
    row = await (
        await conn.execute(
            "SELECT after_key FROM system_object_sweep_cursors WHERE lane = %s",
            (_SYSTEM_ARTIFACT_CURSOR_LANE,),
        )
    ).fetchone()
    if row is None:
        raise RuntimeError("system_object_sweep_cursors has no row-backed lane")
    return row[0]


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
    require_top_level_transaction(conn, "reclaim closed-Investigation build artifacts")
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM investigations "
            "WHERE cleanup_pending_at IS NOT NULL AND cleanup_pending_at < now() - %s "
            "ORDER BY id",
            (grace,),
        )
        investigation_ids = [row[0] for row in await cur.fetchall()]
    deleted = 0
    generation_candidates = await _generation_candidates(
        conn, closed_grace=grace, limit=_BUILD_GENERATIONS_PER_PASS
    )
    generations_by_investigation: dict[UUID, list[UUID]] = {}
    for investigation_id, generation in generation_candidates:
        generations_by_investigation.setdefault(investigation_id, []).append(generation)
    for investigation_id in investigation_ids:
        for generation in generations_by_investigation.get(investigation_id, []):
            deleted += await _reclaim_generation(
                conn,
                cast("ExactArtifactObjectDeleter", store),
                investigation_id,
                generation,
            )
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT a.id, a.object_key FROM artifacts a JOIN runs r ON r.id = a.owner_id "
                "WHERE a.owner_kind = 'runs' AND a.retention_class = ANY(%s) "
                "AND r.investigation_id = %s",
                (list(_BUILD_RETENTION_CLASSES), investigation_id),
            )
            candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
        retired, drained = await _retire_artifact_candidates(
            conn, store, candidates, "investigation"
        )
        deleted += retired
        async with conn.transaction():
            remaining_generation = await (
                await conn.execute(
                    "SELECT 1 FROM investigation_builds WHERE investigation_id = %s LIMIT 1",
                    (investigation_id,),
                )
            ).fetchone()
        if drained and remaining_generation is None:
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
    require_top_level_transaction(conn, "reclaim expired build artifacts")
    deleted = 0
    for investigation_id, generation in await _generation_candidates(
        conn, expired=True, limit=_BUILD_GENERATIONS_PER_PASS
    ):
        deleted += await _reclaim_generation(
            conn, cast("ExactArtifactObjectDeleter", store), investigation_id, generation
        )
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key FROM artifacts "
            "WHERE owner_kind = 'runs' AND retention_class = ANY(%s) AND created_at < now() - %s",
            (list(_BUILD_RETENTION_CLASSES), retention),
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    retired, _ = await _retire_artifact_candidates(conn, store, candidates, "expired build")
    deleted += retired
    if deleted:
        _log.info("reconciler: GC'd %d build artifact(s) past TTL", deleted)
    return deleted
