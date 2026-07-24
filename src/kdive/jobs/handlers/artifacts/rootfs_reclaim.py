"""Worker handler for the internal ``reclaim_investigation_rootfs`` job (ADR-0442, #1522).

Reclaims an investigation's committed uploaded-rootfs bases: for each due ``artifacts`` row, the
ADR-0441 §6 liveness gate, then the staged base unlink, the object delete, and the row delete —
in that order (ADR-0442 §4). The work runs on the worker rather than the reconciler because the
worker created the staging tree: on a host-process local-libvirt deployment it runs as root while
the reconciler runs as the invoking user, so a reconciler-side unlink raises ``PermissionError``
after the object is already gone (#1522). Co-location is structural here — the worker that claims
a local-libvirt job is the libvirt host, the same assumption ``provision``'s staging already
makes — so the removed stat-based probe has nothing left to answer.

Each checksum's gate-and-reclaim runs in one transaction under the ``INVESTIGATION`` advisory lock
that System bind holds transaction-scoped until its row commits, so a bind either is seen as a
pre-overlay referencer (pinning the base) or waits behind the reclaim.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.content_address import rootfs_object_token
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ReclaimInvestigationRootfsPayload, load_payload
from kdive.providers.shared.runtime_paths import (
    ROOTFS_DIR,
    UPLOADS_DIR,
    overlay_name,
    staged_rootfs_path,
)

_log = logging.getLogger(__name__)

#: Wall-clock budget for one object-store delete. The delete runs inside the transaction holding the
#: ``INVESTIGATION`` advisory lock, and the TTL backstop reclaims **live** ``open``/``active``
#: investigations, so an untimed store call could stall a bind, a close, or a ``runs.create`` for as
#: long as the client's own retry budget. A timeout is treated like any other real fault (defer the
#: checksum, keep the row): the request may still land, which is harmless because the retry is
#: 404-tolerant.
_STORE_DELETE_TIMEOUT_S = 10.0

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
_DUE_ROOTFS_ROW_SQL = (
    "SELECT object_key FROM artifacts WHERE id = %s AND owner_id = %s "
    "AND owner_kind = 'investigations' AND retention_class = 'rootfs'"
)
_REMAINING_ROOTFS_ROWS_SQL = (
    "SELECT 1 FROM artifacts "
    "WHERE owner_kind = 'investigations' AND retention_class = 'rootfs' AND owner_id = %s LIMIT 1"
)


class ArtifactObjectDeleter(Protocol):
    """The object-store delete surface the rootfs reclaim needs."""

    def delete(self, key: str) -> None: ...


def _overlay_pins_base(system_id: object, *, rootfs_dir: str) -> bool:
    """Condition (a): whether ``system_id``'s per-System overlay file pins the base (ADR-0441 §6).

    A definite ``FileNotFoundError`` reads as "overlay genuinely gone" (not a pin) — on the worker
    the overlay root is the host's own, so an absent overlay is the real post-state, not a
    visibility gap. Any **other** stat fault is fail-closed — treated as present (a pin) — so a
    transient probe error defers the checksum rather than unlinking a base under a live overlay.
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
    object or the row (ADR-0442 §4): neither SENSITIVE copy may be dropped while the local base
    survives.
    """
    dest = staged_rootfs_path(investigation_id, token, upload_dir=Path(uploads_dir))
    with suppress(FileNotFoundError):
        dest.unlink()


def sweep_investigation_staging_dir(uploads_dir: str, investigation_id: UUID) -> None:
    """Glob-unlink stale ``*.partial`` then remove the now-empty per-investigation staging dir.

    Best-effort (ADR-0441 §5): a crash-orphaned ``<token>.*.partial`` no row owns is unlinked here
    as the backstop to the live fetcher's opportunistic cleanup, **before** the empty-dir removal
    (else a leftover partial keeps the dir non-empty forever). Called only once **no** rootfs row
    remains for the investigation, so a remaining row's in-flight download is never clobbered.
    """
    inv_dir = Path(uploads_dir) / str(investigation_id)
    with suppress(OSError):
        for partial in inv_dir.glob("*.partial"):
            partial.unlink(missing_ok=True)
    with suppress(OSError):
        inv_dir.rmdir()


async def _reclaim_one_checksum(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    *,
    artifact_id: UUID,
    investigation_id: UUID,
    rootfs_dir: str,
    uploads_dir: str,
) -> bool | None:
    """Gate and reclaim one due row under the investigation lock (ADR-0442 §3/§4).

    Returns ``True`` when the checksum drained, ``False`` on a real fault (the caller fails the job
    once every other checksum has been attempted), and ``None`` when there was nothing to do — the
    row already drained, or the gate pins the base, which is the expected steady state and not an
    error. Order is staged base -> object -> row, so a fault leaves the re-downloadable copy rather
    than an unreclaimable local base, and the row (the worklist anchor) outlives both.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        async with conn.cursor() as cur:
            await cur.execute(_DUE_ROOTFS_ROW_SQL, (artifact_id, investigation_id))
            row = await cur.fetchone()
        if row is None:
            return None
        object_key = str(row[0])
        token = _rootfs_token_from_key(object_key)
        if not await rootfs_base_reclaimable(conn, investigation_id, token, rootfs_dir=rootfs_dir):
            return None
        try:
            await asyncio.to_thread(_unlink_staged_base, uploads_dir, investigation_id, token)
        except OSError:
            _log.warning(
                "unlinking the staged rootfs base for %s failed; nothing reclaimed for it",
                object_key,
                exc_info=True,
            )
            return False
        try:
            await asyncio.wait_for(
                asyncio.to_thread(store.delete, object_key), timeout=_STORE_DELETE_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - a real store fault or timeout defers before the row
            _log.warning(
                "deleting the investigation rootfs object %s failed or timed out; its row is kept",
                object_key,
                exc_info=True,
            )
            return False
        await conn.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
    return True


async def _finish_drained_investigation(
    conn: AsyncConnection, investigation_id: UUID, *, uploads_dir: str
) -> None:
    """Sweep the staging dir and clear the close marker once no rootfs row remains (ADR-0442 §7).

    Replaces the sweep's single-pass ``drained`` flag with a read of the real post-state, which is
    what makes the bookkeeping correct across a worker that died mid-reclaim, a stale due-set, and a
    concurrent finalize: each is just a different answer to "are there rows left?". Clearing is
    unconditional on the drain rather than on which sweep enqueued the job — a TTL job only runs
    against an ``open``/``active`` investigation, whose marker is already NULL.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        async with conn.cursor() as cur:
            await cur.execute(_REMAINING_ROOTFS_ROWS_SQL, (investigation_id,))
            if await cur.fetchone() is not None:
                return
        await asyncio.to_thread(sweep_investigation_staging_dir, uploads_dir, investigation_id)
        await conn.execute(
            "UPDATE investigations SET rootfs_cleanup_pending_at = NULL WHERE id = %s",
            (investigation_id,),
        )


async def reclaim_investigation_rootfs_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    artifact_store: ArtifactObjectDeleter,
    rootfs_dir: str = ROOTFS_DIR,
    uploads_dir: str = UPLOADS_DIR,
) -> str | None:
    """Reclaim an investigation's due uploaded-rootfs bases, then settle its drain bookkeeping.

    A checksum the liveness gate pins is skipped and the job still succeeds — pinning is the
    expected steady state for the whole grace window, and dead-lettering the per-investigation
    reclaim slot on it would disable reclaim exactly when it is needed. A **real** unlink or store
    fault is different: it means SENSITIVE bytes are not being reclaimed, so the remaining
    checksums are still attempted and the job then fails, surfacing durably in the ``jobs`` table
    instead of as a log line that repeats every pass (#1522).

    Returns the number of drained checksums as the job's ``result_ref``.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` when any checksum hit a real unlink or
            store fault.
    """
    payload = load_payload(job, ReclaimInvestigationRootfsPayload)
    investigation_id = UUID(payload.investigation_id)
    reclaimed = 0
    faults = 0
    for raw_artifact_id in payload.artifact_ids:
        outcome = await _reclaim_one_checksum(
            conn,
            artifact_store,
            artifact_id=UUID(raw_artifact_id),
            investigation_id=investigation_id,
            rootfs_dir=rootfs_dir,
            uploads_dir=uploads_dir,
        )
        if outcome is True:
            reclaimed += 1
        elif outcome is False:
            faults += 1
    await _finish_drained_investigation(conn, investigation_id, uploads_dir=uploads_dir)
    if reclaimed:
        _log.info(
            "reclaimed %d uploaded rootfs base(s) for investigation %s", reclaimed, investigation_id
        )
    if faults:
        raise CategorizedError(
            f"{faults} uploaded rootfs base(s) of investigation {investigation_id} could not be "
            "reclaimed; the staged base or its object survives and the artifacts row is retained",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return str(reclaimed)


def register_handlers(registry: HandlerRegistry, *, artifact_store: ArtifactObjectDeleter) -> None:
    """Bind the ``reclaim_investigation_rootfs`` job handler with its object-store dep."""
    registry.register(
        JobKind.RECLAIM_INVESTIGATION_ROOTFS,
        lambda conn, job: reclaim_investigation_rootfs_handler(
            conn, job, artifact_store=artifact_store
        ),
    )
