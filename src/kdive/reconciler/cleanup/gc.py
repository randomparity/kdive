"""Garbage-collection style reconciler repairs."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.content_address import rootfs_object_token
from kdive.domain.capacity.state import (
    ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES,
    SystemState,
)
from kdive.domain.errors import CategorizedError
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import DumpVolumeReaper
from kdive.providers.shared.runtime_paths import overlay_name, staged_rootfs_path
from kdive.reconciler.repairs.allocations import has_active_capture_job
from kdive.reconciler.repairs.systems import gone_system_state_values

_log = logging.getLogger(__name__)

#: The state values of the pre-overlay/re-materialize referencer states — condition (b) of the
#: investigation-rootfs reclaim gate (ADR-0441 §6). A referencer in one of these pins the base even
#: with its overlay file momentarily absent.
_PRE_OVERLAY_STATE_VALUES: frozenset[str] = frozenset(
    s.value for s in ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES
)

_ROOTFS_REFERENCERS_SQL = (
    "SELECT id, state, provisioning_profile FROM systems "
    "WHERE investigation_id = %s AND state <> %s"
)

_CLOSE_DRIVEN_INV_SQL = (
    "SELECT id FROM investigations "
    "WHERE rootfs_cleanup_pending_at IS NOT NULL AND rootfs_cleanup_pending_at < now() - %s"
)
_INV_ROOTFS_OBJECTS_SQL = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'investigations' AND retention_class = 'rootfs' AND owner_id = %s"
)
#: TTL backstop scope: committed investigation-rootfs objects past retention on a **never-closed**
#: investigation (``open``/``active`` — a closed one is the close-driven sweep's job, keyed on the
#: since-cleared marker; ADR-0441 §6).
_TTL_ROOTFS_OBJECTS_SQL = (
    "SELECT a.id, a.object_key, a.owner_id FROM artifacts a "
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


def rootfs_dir_accessible(rootfs_dir: str) -> bool:
    """Per-pass fail-closed probe: whether a staging/overlay root directory is accessible.

    Models :func:`console_rotation._boot_id`'s degradation (ADR-0441 §6, AC-8i): any ``os.stat``
    failure — absent, unreadable, or a non-co-located reconciler — degrades to ``False`` so the
    caller defers the **whole** pass rather than reading a missing root as "all overlays gone" and
    mass-deleting live SENSITIVE bases. A path that exists but is not a directory is also unsafe.
    """
    try:
        return stat.S_ISDIR(os.stat(rootfs_dir).st_mode)
    except OSError:
        return False


def _overlay_pins_base(system_id: object, *, rootfs_dir: str) -> bool:
    """Condition (a): whether ``system_id``'s per-System overlay file pins the base (ADR-0441 §6).

    Called only under an already-accessible ``rootfs_dir`` (the per-pass gate ran first), so a
    definite ``FileNotFoundError`` reads as "overlay genuinely gone" (not a pin). Any **other**
    stat fault is fail-closed — treated as present (a pin) — so a transient probe error defers the
    checksum rather than unlinking a base under a possibly-live overlay.
    """
    overlay = os.path.join(rootfs_dir, overlay_name(str(system_id)))
    try:
        os.stat(overlay)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _references_token(profile: object, token: str) -> bool:
    """Whether a stored ``provisioning_profile`` references the upload rootfs of ``token``.

    Parses the raw JSON rootfs ref (ADR-0441 §6): only ``{"kind":"upload","checksum_sha256": C}``
    whose ``C`` transcodes to ``token`` is a referencer. An unparseable profile, one with no rootfs,
    or a ``catalog``/``local``/different-checksum ref is **not** a referencer of ``token`` — so one
    unrelated live System never pins this base.
    """
    if not isinstance(profile, dict):
        return False
    provider = profile.get("provider")
    section = provider.get("local-libvirt") if isinstance(provider, dict) else None
    rootfs = section.get("rootfs") if isinstance(section, dict) else None
    if not isinstance(rootfs, dict) or rootfs.get("kind") != "upload":
        return False
    checksum = rootfs.get("checksum_sha256")
    if not isinstance(checksum, str):
        return False
    try:
        return rootfs_object_token(checksum) == token
    except CategorizedError:
        return False


async def rootfs_base_reclaimable(
    conn: AsyncConnection, investigation_id: UUID, token: str, *, rootfs_dir: str
) -> bool:
    """Whether checksum ``token``'s base can be reclaimed: **no** referencing System pins it.

    Enumerates ``systems WHERE investigation_id=<inv> AND state <> 'torn_down'``, keeps only the
    real referencers of ``token`` (:func:`_references_token`), and pins the base if **any** of them
    is either in a pre-overlay/re-materialize state (condition (b)) or has its overlay file present
    (condition (a), :func:`_overlay_pins_base`). Reclaimable only when none pin (ADR-0441 §6).

    Assumes ``rootfs_dir`` is accessible — the caller runs the per-pass
    :func:`rootfs_dir_accessible` fail-closed gate first and defers the whole pass otherwise.
    """
    async with conn.cursor() as cur:
        await cur.execute(_ROOTFS_REFERENCERS_SQL, (investigation_id, SystemState.TORN_DOWN.value))
        rows = await cur.fetchall()
    for system_id, state, profile in rows:
        if not _references_token(profile, token):
            continue
        if state in _PRE_OVERLAY_STATE_VALUES:
            return False
        if _overlay_pins_base(system_id, rootfs_dir=rootfs_dir):
            return False
    return True


def _rootfs_token_from_key(object_key: str) -> str:
    """Extract the content-address token from a ``rootfs-<token>`` investigation object key."""
    return object_key.rsplit("/", 1)[-1].removeprefix("rootfs-")


def _unlink_staged_base(uploads_dir: str, investigation_id: UUID, token: str) -> None:
    """Unlink the staged base for ``(investigation, token)``; ``ENOENT`` is the achieved post-state.

    Any **other** ``OSError`` propagates so the caller defers the whole checksum before deleting the
    row (ADR-0441 §6): the base file must never be stranded without its ``artifacts`` handle.
    """
    dest = staged_rootfs_path(investigation_id, token, upload_dir=Path(uploads_dir))
    try:
        dest.unlink()
    except FileNotFoundError:
        return


def _sweep_investigation_staging_dir(uploads_dir: str, investigation_id: UUID) -> None:
    """Glob-unlink stale ``*.partial`` then remove the now-empty per-investigation staging dir.

    Best-effort (ADR-0441 §5): a crash-orphaned ``<token>.*.partial`` no row owns is unlinked here
    as the backstop to the live fetcher's opportunistic cleanup, **before** the empty-dir removal
    (else a leftover partial keeps the dir non-empty forever). A dir still holding a base (a
    deferred checksum) is left in place — ``rmdir`` only removes an empty dir.
    """
    inv_dir = Path(uploads_dir) / str(investigation_id)
    with suppress(OSError):
        for partial in inv_dir.glob("*.partial"):
            partial.unlink(missing_ok=True)
    with suppress(OSError):
        inv_dir.rmdir()


async def _reclaim_rootfs_checksum(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    *,
    artifact_id: UUID,
    object_key: str,
    investigation_id: UUID,
    uploads_dir: str,
) -> bool:
    """Reclaim one checksum's object + staged file + row in the pinned order (ADR-0441 §6).

    Order: delete the object, unlink the staged base, then delete the ``artifacts`` row **last**
    (fail-loud in txn — the worklist anchor). Both the object delete (idempotent on a 404) and the
    unlink (``ENOENT`` = success) share one fault contract: any **real** fault defers the whole
    checksum (``False``, row kept) *before* the row delete, so a genuine fault never drops the row
    while the SENSITIVE object/file survives. Returns ``True`` when the checksum drained.
    """
    try:
        await asyncio.to_thread(store.delete, object_key)
    except Exception:  # noqa: BLE001 - a real store fault defers before the row delete
        _log.warning(
            "reconciler: deleting investigation rootfs object %s failed; defer + retry next pass",
            object_key,
            exc_info=True,
        )
        return False
    token = _rootfs_token_from_key(object_key)
    try:
        await asyncio.to_thread(_unlink_staged_base, uploads_dir, investigation_id, token)
    except OSError:
        _log.warning(
            "reconciler: unlinking staged rootfs base for %s failed; defer + retry next pass",
            object_key,
            exc_info=True,
        )
        return False
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
    return True


async def _reclaim_object_if_reclaimable(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    *,
    artifact_id: UUID,
    object_key: str,
    investigation_id: UUID,
    rootfs_dir: str,
    uploads_dir: str,
) -> bool:
    """Reclaim one committed rootfs object iff the liveness gate permits; else defer (ADR-0441 §6).

    Returns ``True`` only when the checksum fully drained (object + staged file + row gone). A gate
    that pins the base, or a fault mid-reclaim, returns ``False`` so the caller keeps the marker set
    and retries next pass.
    """
    token = _rootfs_token_from_key(object_key)
    if not await rootfs_base_reclaimable(conn, investigation_id, token, rootfs_dir=rootfs_dir):
        return False
    return await _reclaim_rootfs_checksum(
        conn,
        store,
        artifact_id=artifact_id,
        object_key=object_key,
        investigation_id=investigation_id,
        uploads_dir=uploads_dir,
    )


async def gc_investigation_uploaded_rootfs(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    grace: timedelta,
    *,
    rootfs_dir: str,
    uploads_dir: str,
) -> int:
    """Reclaim closed investigations' uploaded rootfs bases past ``grace`` (close-driven, ADR-0441).

    Selects investigations by the **dedicated** ``rootfs_cleanup_pending_at`` marker (never the
    build sweep's ``cleanup_pending_at``, so a drained build artifact cannot starve this). For each,
    reclaims every committed ``owner_kind='investigations'``/``retention_class='rootfs'`` object
    whose checksum passes the overlay-absence gate, then — only when **all** its rootfs objects
    drained — sweeps the staging dir and clears its own marker. Fails **closed** per pass: an
    inaccessible ``rootfs_dir``/``uploads_dir`` reclaims nothing (defers), never unlinking a base
    under a live guest.
    """
    if not (rootfs_dir_accessible(rootfs_dir) and rootfs_dir_accessible(uploads_dir)):
        return 0
    async with conn.cursor() as cur:
        await cur.execute(_CLOSE_DRIVEN_INV_SQL, (grace,))
        investigation_ids = [row[0] for row in await cur.fetchall()]
    deleted = 0
    for investigation_id in investigation_ids:
        async with conn.cursor() as cur:
            await cur.execute(_INV_ROOTFS_OBJECTS_SQL, (investigation_id,))
            candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
        drained = True
        for artifact_id, object_key in candidates:
            if await _reclaim_object_if_reclaimable(
                conn,
                store,
                artifact_id=artifact_id,
                object_key=object_key,
                investigation_id=investigation_id,
                rootfs_dir=rootfs_dir,
                uploads_dir=uploads_dir,
            ):
                deleted += 1
            else:
                drained = False
        if drained:
            await asyncio.to_thread(_sweep_investigation_staging_dir, uploads_dir, investigation_id)
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE investigations SET rootfs_cleanup_pending_at = NULL WHERE id = %s",
                    (investigation_id,),
                )
    if deleted:
        _log.info("reconciler: GC'd %d closed-investigation uploaded rootfs base(s)", deleted)
    return deleted


async def gc_expired_investigation_rootfs(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    retention: timedelta,
    *,
    rootfs_dir: str,
    uploads_dir: str,
) -> int:
    """Reclaim never-closed investigations' uploaded rootfs bases past ``retention`` (TTL backstop).

    The mandatory backstop (ADR-0441 §6): a never-closed investigation would otherwise accumulate
    SENSITIVE bases forever. Same overlay-absence gate and pinned reclaim as the close-driven sweep,
    but gated on ``artifacts.created_at`` and scoped to ``open``/``active`` investigations. Fails
    closed per pass on an inaccessible root.
    """
    if not (rootfs_dir_accessible(rootfs_dir) and rootfs_dir_accessible(uploads_dir)):
        return 0
    async with conn.cursor() as cur:
        await cur.execute(_TTL_ROOTFS_OBJECTS_SQL, (retention,))
        candidates = [(row[0], str(row[1]), row[2]) for row in await cur.fetchall()]
    deleted = 0
    touched: set[UUID] = set()
    for artifact_id, object_key, investigation_id in candidates:
        touched.add(investigation_id)
        if await _reclaim_object_if_reclaimable(
            conn,
            store,
            artifact_id=artifact_id,
            object_key=object_key,
            investigation_id=investigation_id,
            rootfs_dir=rootfs_dir,
            uploads_dir=uploads_dir,
        ):
            deleted += 1
    for investigation_id in touched:
        if not await _investigation_has_rootfs_objects(conn, investigation_id):
            await asyncio.to_thread(_sweep_investigation_staging_dir, uploads_dir, investigation_id)
    if deleted:
        _log.info("reconciler: GC'd %d uploaded rootfs base(s) past TTL", deleted)
    return deleted


async def _investigation_has_rootfs_objects(conn: AsyncConnection, investigation_id: UUID) -> bool:
    """Return ``True`` while any committed uploaded-rootfs object row remains for the investigation.

    Gates the TTL staging-dir sweep (glob-unlink ``*.partial`` + rmdir): a live provider fetch
    resolves against a committed row before it writes ``<token>.<uuid>.partial``, so a remaining row
    means a download may be in flight and its partial must not be clobbered. Only when **no** rootfs
    row remains — every past-TTL base drained and no not-yet-TTL base still in use — is a stray
    ``*.partial`` necessarily a crash orphan, matching the close-driven sweep's ``if drained`` guard
    (ADR-0441 §5/§6).
    """
    async with conn.cursor() as cur:
        await cur.execute(_INV_ROOTFS_OBJECTS_SQL, (investigation_id,))
        return await cur.fetchone() is not None


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
