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
from kdive.providers.shared.staging_partials import unlink_partial_if_unheld

_log = logging.getLogger(__name__)

#: Wall-clock budget for one object-store delete. The delete runs inside the transaction holding the
#: ``INVESTIGATION`` advisory lock, and the TTL backstop reclaims **live** ``open``/``active``
#: investigations, so an untimed store call could stall a bind, a close, or a ``runs.create`` for as
#: long as the client's own retry budget. A timeout is treated like any other real fault: defer the
#: checksum and keep its row, so the row-last contract holds. The abandoned request may still land,
#: leaving a row whose object *and* staged base are both gone until the next reclaim drains it — a
#: bounded residual, recorded in ADR-0442, not a silently-benign case.
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


def sweep_investigation_staging_dir(uploads_dir: str, investigation_id: UUID) -> bool:
    """Empty a drained investigation's staging dir: unheld ``*.partial``, then unowned bases.

    Best-effort (ADR-0441 §5): a crash-orphaned ``<token>.*.partial`` no row owns is unlinked here
    as the backstop to the live fetcher's opportunistic cleanup, **before** the empty-dir removal
    (else a leftover partial keeps the dir non-empty forever).

    What it must not collect is a partial some fetcher is still writing. ADR-0442 §7 justified the
    unconditional unlink on this running only once **no** rootfs row remains for the investigation,
    "so a remaining row's in-flight download is never clobbered" — but that is a *derived* claim and
    the derivation does not hold. The row count reaches zero only because
    :func:`rootfs_base_reclaimable` classified the base as unpinned, and that gate reads the System
    row's state column plus overlay-file presence: :data:`_ROOTFS_REFERENCERS_SQL` excludes
    ``torn_down`` outright, ``failed`` is outside
    :data:`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`, and a *provisioning* System has no overlay file
    yet. ``PROVISIONING -> TORN_DOWN`` and ``PROVISIONING -> FAILED`` are both legal transitions,
    the download runs detached under ``asyncio.to_thread`` and cannot be cancelled, and nothing
    serializes the two — the fetch takes only its per-(investigation, checksum) session lock, never
    the ``INVESTIGATION`` lock this job holds. So a concurrent teardown drops the pin, this reclaim
    deletes the last row, and the next statement would sweep a **live** partial (#1544).

    Liveness is therefore asked of the kernel, exactly as the fetch-side sweep asks it (ADR-0446):
    a live writer holds an exclusive ``flock`` on its own partial and a candidate that cannot be
    locked is skipped. Do not re-derive the safety from the row count, and do not narrow the glob —
    it deliberately covers every token in the directory, not one base's.

    The ``suppress`` covers the directory walk only; every per-candidate fault is handled inside
    :func:`unlink_partial_if_unheld`, so one unsweepable file cannot truncate the pass.

    A staged **base** found here is unowned by construction and is collected too
    (:func:`_unlink_unowned_base`), which is what keeps the gate above from trading one leak for a
    worse one: the writer it now protects can run to completion and publish onto ``<token>.qcow2``
    whose row this reclaim already deleted, and nothing else in the tree collects a row-less base.

    Returns:
        Whether a live writer's ``flock`` left a partial behind. That partial also keeps ``inv_dir``
        non-empty, so the ``rmdir`` below fails with ``ENOTEMPTY`` — the deliberate post-state of a
        live-held skip, not a surprise (ADR-0452 §5). The caller retains the drain marker on it so a
        later pass finishes the job, which is also the pass that collects whatever that writer
        published in the meantime.
    """
    inv_dir = Path(uploads_dir) / str(investigation_id)
    held = False
    with suppress(OSError):
        for partial in inv_dir.glob("*.partial"):
            if unlink_partial_if_unheld(partial, unlink_when_unlockable=True):
                held = True
    with suppress(OSError):
        for base in inv_dir.glob("*.qcow2"):
            _unlink_unowned_base(base)
    with suppress(OSError):
        inv_dir.rmdir()
    return held


def _unlink_unowned_base(base: Path) -> None:
    """Collect a staged base left in a drained investigation's staging dir (ADR-0452 §6, #1559).

    Reached only from the drain tail, which runs under the ``INVESTIGATION`` lock and only once
    :data:`_REMAINING_ROOTFS_ROWS_SQL` returns nothing — so **every** file here is unowned by
    construction, and no System can be running off this base either: an overlay on it would have
    pinned its row through :func:`_overlay_pins_base`, and a row that survives ends the drain tail
    before this runs. That precondition is the whole licence for an ungated unlink, so do not move
    this call anywhere the row count has not just been read under the lock.

    It should also never fire. :func:`_unlink_staged_base` removes each base as its own row drains,
    so a base surviving to here means one was published *without* a row — the shape the ``flock``
    gate above makes reachable, where a doomed fetcher whose System was torn down mid-download
    completes anyway and ``os.replace``\\ s onto a path this reclaim already emptied. ``WARNING``
    rather than a silent unlink, because a SENSITIVE base of up to the 50 GiB canonical cap arriving
    after its own reclaim is worth seeing, and because it is otherwise indistinguishable from the
    directory simply being empty.
    """
    try:
        base.unlink(missing_ok=True)
    except OSError as err:
        _log.warning(
            "could not unlink the unowned staged rootfs base %s (%s); it holds no artifacts row, "
            "and no sweep will revisit it once this investigation's drain marker clears",
            base,
            err.strerror,
        )
        return
    _log.warning(
        "collected the staged rootfs base %s, which outlived the artifacts row that owned it; a "
        "fetcher published it after its checksum had already been reclaimed (#1544)",
        base,
    )


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
    keyed on the drain rather than on which sweep enqueued the job — a TTL job only runs against an
    ``open``/``active`` investigation, whose marker is already NULL.

    The marker is retained for exactly one non-drained case: the sweep skipped a partial a live
    writer still ``flock``\\ s (ADR-0452 §4). Neither neighbouring choice is right. Clearing
    unconditionally would leave that partial with **no** collector — this marker is the only thing
    that re-enqueues a reclaim for a closed investigation, and the fetch-side opportunistic sweep
    only fires on the next fetch of that base, which never comes once the investigation is closed —
    so a holder killed mid-download would leak its multi-GiB SENSITIVE partial permanently, which is
    what this backstop exists to prevent. Retaining on *every* non-drain would be worse the other
    way: an unopenable or unlinkable partial is permanent until an operator acts, and pinning the
    marker on it resurrects the never-clearing marker and the re-fail-every-pass loop ADR-0442 was
    written about. A held ``flock`` is the one outcome the kernel guarantees is transient — it is
    released when the holding descriptor closes, including on ``SIGKILL`` — so the retry converges.

    **That retry exists on the close-driven lane only, and the asymmetry is real.** A TTL job runs
    against an ``open``/``active`` investigation whose marker is already NULL, so retaining is a
    no-op there — and that lane's own worklist (``reconciler.cleanup.gc._TTL_ROOTFS_OBJECTS_SQL``)
    is a pure ``artifacts`` query over rows this job just deleted, so it cannot re-select the
    investigation either. A partial skipped on the TTL path therefore waits for the investigation to
    close. It is narrow — the fetcher unlinks its own partial in its ``finally``, so only a *killed*
    holder leaves one — and :func:`sweep_investigation_staging_dir` still empties the rest of the
    directory in that same pass. #1565 tracks giving that lane a trigger of its own; the close
    marker is deliberately not overloaded onto an open investigation here, because it is durable
    state whose meaning is "this investigation was closed and its rootfs is being reclaimed".
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        async with conn.cursor() as cur:
            await cur.execute(_REMAINING_ROOTFS_ROWS_SQL, (investigation_id,))
            if await cur.fetchone() is not None:
                return
        held = await asyncio.to_thread(
            sweep_investigation_staging_dir, uploads_dir, investigation_id
        )
        if held:
            # WARNING, not INFO, and worded for the *observation* and the achieved post-state rather
            # than for the UPDATE below — which is a no-op on the TTL path, whose investigations
            # carry a NULL marker, so "the marker is kept" would be false exactly there. It also
            # does not predict how that writer ends: on the gzip path its remaining ranged GETs 404
            # against the object this reclaim deleted, while on the identity path the response body
            # is already open and it usually *succeeds* — publishing a base with no row, which the
            # deferred pass collects. Asserting either would be the inference-as-invariant this
            # change removes from the sweep's own WARNING one file over.
            _log.warning(
                "investigation %s has no rootfs rows left, but a live writer still holds a staging "
                "partial; keeping its staging directory and deferring the drain until that writer "
                "exits. Its System is already failed or torn down — that is the only way the pin "
                "dropped — so the fetch is doomed either way, and anything it publishes there is "
                "unowned and is collected by the deferred pass",
                investigation_id,
            )
            return
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
    fault is different: it means SENSITIVE bytes are not being reclaimed, so the job fails,
    surfacing durably in the ``jobs`` table instead of as a log line that repeats every pass
    (#1522).

    The first real fault **ends the loop** rather than attempting the remaining checksums. A store
    that is refusing or timing out is a store-wide condition, and the object-delete budget is a
    per-call one, so pressing on would burn that budget once per remaining checksum while the worker
    slot — and the ``INVESTIGATION`` lock — stay held. Nothing is lost: the surviving checksums keep
    their rows and are re-attempted by the next sweep, which is the retry loop.

    Returns the number of drained checksums as the job's ``result_ref``.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` when a checksum hit a real unlink or store
            fault.
    """
    payload = load_payload(job, ReclaimInvestigationRootfsPayload)
    investigation_id = UUID(payload.investigation_id)
    reclaimed = 0
    faulted = False
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
            faulted = True
            break
    await _finish_drained_investigation(conn, investigation_id, uploads_dir=uploads_dir)
    if reclaimed:
        _log.info(
            "reclaimed %d uploaded rootfs base(s) for investigation %s", reclaimed, investigation_id
        )
    if faulted:
        raise CategorizedError(
            f"reclaiming an uploaded rootfs base of investigation {investigation_id} failed; its "
            "artifacts row is retained and the remaining bases are deferred to the next sweep",
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
