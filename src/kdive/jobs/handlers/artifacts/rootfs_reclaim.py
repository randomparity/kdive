"""Reclaim committed uploaded-rootfs bases on their staging worker.

For each due artifact, capture a bounded immutable object history without a database transaction.
Under the investigation lock, re-read the artifact, reject live System and fetch pins, unlink the
staged base, and retire the row atomically. Delete only the captured object versions after commit,
without holding the lock or a database connection (ADR-0441, ADR-0442, ADR-0524).

System binding and rootfs publication take the same investigation lock, so their state is either
visible to the reclaim gates or serialized after retirement. Fetches do not take that lock.
Reclaim therefore checks a job-fenced database lease, when one was acquired, and a same-host
``flock`` on existing staging partials. Lease acquisition is best-effort and an unleased fetch
before partial creation remains a logged, fail-open residual (ADR-0495, ADR-0515, ADR-0522).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.content_address import rootfs_object_token
from kdive.artifacts.storage import VersionBatch
from kdive.db.locks import LockScope, advisory_xact_lock, require_top_level_transaction
from kdive.domain.capacity.state import ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ReclaimInvestigationRootfsPayload, load_payload
from kdive.providers.shared.rootfs_fetch_leases import (
    fetch_lease_pins_base,
    reap_dead_fetch_leases,
)
from kdive.providers.shared.runtime_paths import (
    ROOTFS_DIR,
    STAGED_ROOTFS_MARKER_SUFFIX,
    UPLOADS_DIR,
    overlay_name,
    staged_rootfs_marker_path,
    staged_rootfs_path,
)
from kdive.providers.shared.staging_partials import (
    live_writer_holds_partial,
    unlink_partial_if_unheld,
)

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
_DUE_ROOTFS_ROW_SQL = (
    "SELECT object_key FROM artifacts WHERE id = %s AND owner_id = %s "
    "AND owner_kind = 'investigations' AND retention_class = 'rootfs'"
)
_INV_ROOTFS_KEYS_SQL = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'investigations' AND retention_class = 'rootfs' AND owner_id = %s"
)


class ArtifactObjectDeleter(Protocol):
    """The bounded exact-version surface investigation-rootfs reclaim needs."""

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch: ...
    def delete_batch(self, batch: VersionBatch) -> bool: ...


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


def _referenced_token(profile: object) -> str | None:
    """The upload-rootfs content-address token a stored ``provisioning_profile`` references.

    Parses the raw JSON rootfs ref (ADR-0441 §6): only ``{"kind":"upload","checksum_sha256": C}``
    names a token. An unparseable profile, one with no rootfs, or a ``catalog``/``local`` ref
    references no uploaded base at all — so one unrelated live System never pins one.
    """
    if not isinstance(profile, dict):
        return None
    provider = profile.get("provider")
    section = provider.get("local-libvirt") if isinstance(provider, dict) else None
    rootfs = section.get("rootfs") if isinstance(section, dict) else None
    if not isinstance(rootfs, dict) or rootfs.get("kind") != "upload":
        return None
    checksum = rootfs.get("checksum_sha256")
    if not isinstance(checksum, str):
        return None
    try:
        return rootfs_object_token(checksum)
    except CategorizedError:
        return None


async def pinned_rootfs_tokens(
    conn: AsyncConnection, investigation_id: UUID, *, rootfs_dir: str
) -> frozenset[str]:
    """Every uploaded-base token a live System of ``investigation_id`` pins (ADR-0441 §6).

    Enumerates ``systems WHERE investigation_id=<inv> AND state <> 'torn_down'``, resolves each
    one's referenced token (:func:`_referenced_token`), and pins that token when the System is
    either in a pre-overlay/re-materialize state (condition (b)) or has its overlay file present
    (condition (a), :func:`_overlay_pins_base`).

    The set form lets the filesystem-keyed sweep classify every directory entry from one System
    enumeration while the caller holds the investigation lock (ADR-0494).
    """
    async with conn.cursor() as cur:
        await cur.execute(_ROOTFS_REFERENCERS_SQL, (investigation_id, SystemState.TORN_DOWN.value))
        rows = await cur.fetchall()
    pinned: set[str] = set()
    for system_id, state, profile in rows:
        token = _referenced_token(profile)
        if token is None or token in pinned:
            continue
        if state in _PRE_OVERLAY_STATE_VALUES or _overlay_pins_base(
            system_id, rootfs_dir=rootfs_dir
        ):
            pinned.add(token)
    return frozenset(pinned)


async def rootfs_base_reclaimable(
    conn: AsyncConnection, investigation_id: UUID, token: str, *, rootfs_dir: str
) -> bool:
    """Whether checksum ``token``'s base can be reclaimed: **no** referencing System pins it.

    Uses :func:`pinned_rootfs_tokens` so row-driven and filesystem-driven reclaim share one pin
    definition (ADR-0494).
    """
    return token not in await pinned_rootfs_tokens(conn, investigation_id, rootfs_dir=rootfs_dir)


def _rootfs_token_from_key(object_key: str) -> str:
    """Extract the content-address token from a ``rootfs-<token>`` investigation object key."""
    return object_key.rsplit("/", 1)[-1].removeprefix("rootfs-")


def _unlink_staged_base(uploads_dir: str, investigation_id: UUID, token: str) -> None:
    """Unlink the staged base and its completion marker; ``ENOENT`` is the achieved post-state.

    Any **other** ``OSError`` propagates — from either unlink, which is why they share one region —
    so the caller defers the whole checksum before retiring the row and therefore before the later
    exact-version delete (ADR-0442 §4): neither SENSITIVE copy may be dropped while the local base
    survives.

    The completion marker goes first. Interruption may leave an unmarked base, which the reuse gate
    rejects and re-stages, but never a marker attesting to a removed base (ADR-0451).
    """
    dest = staged_rootfs_path(investigation_id, token, upload_dir=Path(uploads_dir))
    with suppress(FileNotFoundError):
        staged_rootfs_marker_path(dest).unlink()
    with suppress(FileNotFoundError):
        dest.unlink()


def sweep_investigation_staging_dir(
    uploads_dir: str,
    investigation_id: UUID,
    *,
    protected_tokens: frozenset[str],
    drained: bool,
) -> bool:
    """Collect unowned files and retire an investigation's staging directory when drained.

    The caller holds the ``INVESTIGATION`` lock and supplies every token owned by an artifact row
    or pinned by a live System. Bases and completion markers outside that set are collected on
    every pass. Partials are collected only after all rootfs rows have drained. A held ``flock``
    defers collection; a proven-unheld partial is removed under that lock, while a filesystem that
    cannot support ``flock`` takes the documented last-collector fail-open path (ADR-0452,
    ADR-0494).

    Returns ``True`` when a later pass must retry: a row still survives or a live writer holds a
    partial. A failed ``rmdir`` triggers one bounded re-collection to catch a file published after
    the first enumeration. Permanent filesystem faults and pinned-but-unowned bases are reported
    but do not create an unbounded retry loop.
    """
    inv_dir = Path(uploads_dir) / str(investigation_id)
    if not drained:
        # A surviving row supplies the retry lane and may name an in-flight fetch. Collect only
        # token-classified bases and markers; partial collection waits for a full drain.
        _collect_unowned_bases(inv_dir, protected_tokens)
        _unlink_completion_markers(inv_dir, protected_tokens)
        return True
    outcome = _collect_unprotected(inv_dir, protected_tokens)
    if outcome.held:
        return True
    return _drain_staging_dir(inv_dir, protected_tokens, left_pinned=outcome.left_pinned)


@dataclass(frozen=True, slots=True)
class _SweepOutcome:
    """Observed reasons a staging directory survived one collecting pass.

    These are walk results, not facts inferred from a non-empty ``protected_tokens`` set; the
    directory may already be empty even when rows or pins exist.
    """

    held: bool
    left_pinned: bool


def _collect_unprotected(inv_dir: Path, protected_tokens: frozenset[str]) -> _SweepOutcome:
    """Run all three collectors over ``inv_dir`` and report what the walk observed."""
    held = _unlink_unheld_partials(inv_dir)
    left_pinned = _collect_unowned_bases(inv_dir, protected_tokens)
    _unlink_completion_markers(inv_dir, protected_tokens)
    return _SweepOutcome(held=held, left_pinned=left_pinned)


def _drain_staging_dir(
    inv_dir: Path, protected_tokens: frozenset[str], *, left_pinned: bool
) -> bool:
    """Retire ``inv_dir``, collecting once more if it did not go; return whether to defer.

    One re-pass closes the enumerate-to-``rmdir`` publication window without risking an unbounded
    loop. A newly observed held partial is transient and defers. A pinned base is never unlinked,
    but does not defer because a failed System may pin it permanently; it is reported for operator
    follow-up. Other survivors are reported and the drain settles (ADR-0452, ADR-0494).
    """
    if _try_rmdir(inv_dir) is None:
        return False
    outcome = _collect_unprotected(inv_dir, protected_tokens)
    reason = _try_rmdir(inv_dir)
    if reason is None:
        return False
    if outcome.held:
        return True
    if left_pinned or outcome.left_pinned:
        _log.warning(
            "the rootfs staging directory %s still holds a staged base that no artifacts row owns "
            "but a live System pins; it is left in place rather than unlinked under that System's "
            "overlay, and this investigation's drain marker is cleared — a pin by a failed System "
            "does not heal on its own, so inspect it once that System is gone",
            inv_dir,
        )
        return False
    _warn_undrained_dir(inv_dir, reason)
    return False


def _unlink_unheld_partials(inv_dir: Path) -> bool:
    """Run the liveness gate over every ``*.partial``; return whether a live writer held one.

    The ``try`` covers the directory walk only — every per-candidate fault is handled inside
    :func:`unlink_partial_if_unheld`, so one unsweepable file cannot truncate the pass — and a walk
    that faults is logged rather than swallowed, because this sweep is the last collector and a
    silent empty walk is indistinguishable from a drained directory.
    """
    held = False
    try:
        for partial in inv_dir.glob("*.partial"):
            if unlink_partial_if_unheld(partial, unlink_when_unlockable=True):
                held = True
    except OSError as err:
        _log.warning(
            "could not walk the rootfs staging directory %s for staging partials (%s); anything it "
            "holds is left uncollected",
            inv_dir,
            err.strerror,
        )
    return held


def _collect_unowned_bases(inv_dir: Path, protected_tokens: frozenset[str]) -> bool:
    """Collect every staged base in ``inv_dir`` whose token nothing owns or pins (ADR-0494 §3).

    The base stem is its content-address token. Tokens are unpadded base64url and cannot contain a
    ``.``, so ``Path.stem`` is the same identity written by :func:`staged_rootfs_path` and derived
    from the artifact key by :func:`_rootfs_token_from_key`.

    Returns:
        Whether the walk observed a base whose token is protected.
    """
    left_pinned = False
    try:
        for base in inv_dir.glob("*.qcow2"):
            if base.stem in protected_tokens:
                left_pinned = True
                continue
            _unlink_unowned_base(base)
    except OSError as err:
        _log.warning(
            "could not walk the rootfs staging directory %s for staged bases (%s); anything it "
            "holds is left uncollected",
            inv_dir,
            err.strerror,
        )
    return left_pinned


def _unlink_completion_markers(inv_dir: Path, protected_tokens: frozenset[str]) -> None:
    """Collect completion markers whose token nothing owns or pins (ADR-0451, ADR-0494).

    Marker collection is token-gated rather than ``flock``-gated: a marker belongs to a published
    base, and removing one for a protected base would force an unnecessary re-stage. Successful
    removal is silent; per-candidate and directory-walk faults are reported.
    """
    try:
        for marker in inv_dir.glob(f"*{STAGED_ROOTFS_MARKER_SUFFIX}"):
            if marker.stem in protected_tokens:
                continue
            try:
                marker.unlink(missing_ok=True)
            except OSError as err:
                _log.warning(
                    "could not unlink the staged rootfs completion marker %s (%s); it will keep "
                    "this investigation's staging directory from being removed",
                    marker,
                    err.strerror,
                )
    except OSError as err:
        _log.warning(
            "could not walk the rootfs staging directory %s for completion markers (%s); anything "
            "it holds is left uncollected",
            inv_dir,
            err.strerror,
        )


def _try_rmdir(inv_dir: Path) -> str | None:
    """Retire ``inv_dir``; return ``None`` when it is gone, else why it is not.

    ``rmdir`` distinguishes an empty directory from an unreadable one after ``Path.glob`` has
    yielded no entries for either. ``ENOENT`` is the achieved post-state. Other failures are
    returned so :func:`_drain_staging_dir` can retry collection once before reporting them.
    """
    try:
        inv_dir.rmdir()
    except FileNotFoundError:
        return None
    except OSError as err:
        return err.strerror or str(err)
    return None


def _warn_undrained_dir(inv_dir: Path, reason: str) -> None:
    """Report a staging directory that survived a drain nothing else explains.

    A permanent fault must not retain the drain marker forever, so the caller settles the drain and
    this warning becomes the operator's recovery signal (ADR-0452).
    """
    _log.warning(
        "the rootfs staging directory %s survived its investigation's drain (%s) and no live "
        "writer explains it; its drain marker is cleared regardless, so nothing will revisit "
        "it — inspect it for uncollected SENSITIVE staging files",
        inv_dir,
        reason,
    )


def _unlink_unowned_base(base: Path) -> None:
    """Collect a staged base whose token has no owner or pin (ADR-0452, ADR-0494).

    The caller must hold the ``INVESTIGATION`` lock and classify the token against both artifact
    ownership and live-System pins immediately before this call. A surviving base indicates a
    publish after its row-level reclaim, so successful collection is reported as well as failures.
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


def _live_writer_holds_a_partial(uploads_dir: str, investigation_id: UUID, token: str) -> bool:
    """Whether a live writer provably holds this token's staging partial (ADR-0495).

    This read-only same-host gate complements the durable fetch lease and can only withhold a
    reclaim. It scans only ``<token>.*.partial`` so unrelated downloads do not stall this checksum.
    Only a proven ``flock`` hold returns ``True``; missing directories return ``False`` and other
    walk or lock faults are warned and fail open to avoid permanently pinning the artifact row.
    """
    inv_dir = staged_rootfs_path(investigation_id, token, upload_dir=Path(uploads_dir)).parent
    prefix, suffix = f"{token}.", ".partial"
    try:
        with os.scandir(inv_dir) as entries:
            candidates = [
                Path(entry.path)
                for entry in entries
                if entry.name.startswith(prefix) and entry.name.endswith(suffix)
            ]
    except FileNotFoundError:
        return False
    except OSError as err:
        _log.warning(
            "could not read the rootfs staging directory %s to test for an in-flight download of "
            "%s (%s); reclaiming its checksum anyway, because deferring on a fault that is "
            "permanent until an operator acts would strand this investigation's every checksum",
            inv_dir,
            token,
            err.strerror,
        )
        return False
    return any(live_writer_holds_partial(candidate) for candidate in candidates)


async def _reclaim_one_checksum(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    *,
    artifact_id: UUID,
    investigation_id: UUID,
    rootfs_dir: str,
    uploads_dir: str,
) -> bool | None:
    """Capture one due key, retire its row under lock, then exact-delete after commit.

    Capture the immutable version inventory outside the owner transaction. Under the
    ``INVESTIGATION`` lock, re-read the row, check System pins, the job-fenced fetch lease, and the
    same-host partial ``flock`` in that order, then unlink the staged base and delete the artifact
    row atomically. Exact-delete only the captured identities after commit (ADR-0442, ADR-0524).

    A pin returns ``None`` and retains the staged base, object, and row; the row-driven sweep is the
    retry mechanism. Capture, unlink, and delete faults return ``False``. A committed retirement
    returns ``True`` even when the bounded version batch is incomplete, because the orphan-version
    sweep can rediscover its rowless survivors.
    """
    require_top_level_transaction(conn, "investigation rootfs version capture")
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(_DUE_ROOTFS_ROW_SQL, (artifact_id, investigation_id))
        candidate_row = await cur.fetchone()
    if candidate_row is None:
        return None
    object_key = str(candidate_row[0])
    try:
        batch = await asyncio.to_thread(store.capture_exact_versions, object_key, 1000)
    except Exception:  # noqa: BLE001 - the store boundary owns dependency exception mapping
        _log.warning(
            "capturing investigation rootfs versions for %s failed; nothing reclaimed for it",
            object_key,
            exc_info=True,
        )
        return False

    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        async with conn.cursor() as cur:
            await cur.execute(_DUE_ROOTFS_ROW_SQL, (artifact_id, investigation_id))
            row = await cur.fetchone()
        if row is None:
            return None
        if str(row[0]) != object_key:
            return None
        token = _rootfs_token_from_key(object_key)
        if not await rootfs_base_reclaimable(conn, investigation_id, token, rootfs_dir=rootfs_dir):
            return None
        if await fetch_lease_pins_base(conn, investigation_id, token):
            # The indexed durable lease brackets the narrower filesystem partial window, so check
            # it first. The job succeeds on a pin, making this warning the record of the deferral.
            _log.warning(
                "deferring the reclaim of %s: a rootfs fetch lease held by a live job says a "
                "download of its checksum is in flight, so its staged base, its object and its "
                "artifacts row are all retained; the next sweep retries it once that fetch "
                "releases the lease or its holding job stops being a live claim",
                object_key,
            )
            return None
        if await asyncio.to_thread(
            _live_writer_holds_a_partial, uploads_dir, investigation_id, token
        ):
            # The probe logs the file observation; this line records the resulting retention
            # decision because a pinned checksum is still a successful job outcome.
            _log.warning(
                "deferring the reclaim of %s: a live writer holds a staging partial for its "
                "checksum, so its staged base, its object and its artifacts row are all retained "
                "and the next sweep retries it once that writer exits",
                object_key,
            )
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
        await conn.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
    try:
        complete = await asyncio.to_thread(store.delete_batch, batch)
    except Exception:  # noqa: BLE001 - report the dependency fault after durable row retirement
        _log.warning(
            "deleting captured investigation rootfs versions for %s failed; the row is already "
            "retired and the upload orphan sweep will rediscover every survivor",
            object_key,
            exc_info=True,
        )
        return False
    if not complete:
        _log.info(
            "investigation rootfs %s retained the captured latest version because its 1000-target "
            "history batch was incomplete; the upload orphan sweep will continue it",
            object_key,
        )
    return True


async def _owned_rootfs_tokens(conn: AsyncConnection, investigation_id: UUID) -> frozenset[str]:
    """The content-address tokens ``investigation_id``'s surviving rootfs ``artifacts`` rows own."""
    async with conn.cursor() as cur:
        await cur.execute(_INV_ROOTFS_KEYS_SQL, (investigation_id,))
        return frozenset(_rootfs_token_from_key(str(row[0])) for row in await cur.fetchall())


async def _finish_drained_investigation(
    conn: AsyncConnection, investigation_id: UUID, *, rootfs_dir: str, uploads_dir: str
) -> None:
    """Sweep staging and clear a settled close-driven drain marker.

    Under one ``INVESTIGATION``-locked transaction, reap dead fetch leases, read the current owned
    and pinned token sets, and run the filesystem sweep. Reading post-state here keeps bookkeeping
    correct after stale worklists, interrupted jobs, and concurrent finalization (ADR-0442,
    ADR-0494).

    A surviving artifact row supplies an automatic row-driven retry, whether its cause is transient
    or requires operator repair. A live-held partial is the provably transient filesystem outcome
    and retains the close-driven marker. Pinned bases and permanent filesystem faults are left and
    reported but settle the marker to avoid an endless retry loop. The update is predicated because
    TTL and staging-drain jobs operate on open investigations whose marker is already ``NULL``.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
        # Dead leases are already inert; reap them here to bound retained rows while the relevant
        # investigation lock is held.
        await reap_dead_fetch_leases(conn, investigation_id)
        owned = await _owned_rootfs_tokens(conn, investigation_id)
        pinned = await pinned_rootfs_tokens(conn, investigation_id, rootfs_dir=rootfs_dir)
        deferred = await asyncio.to_thread(
            sweep_investigation_staging_dir,
            uploads_dir,
            investigation_id,
            protected_tokens=owned | pinned,
            drained=not owned,
        )
        if deferred:
            if not owned:
                # A closed investigation needs its marker to retry a held, rowless partial. Open
                # investigations may have no marker, so describe the retained directory rather
                # than claiming an update occurred.
                _log.warning(
                    "investigation %s has no rootfs rows left, but a live writer still holds a "
                    "staging partial; keeping its staging directory and deferring the drain until "
                    "that writer exits. Anything it publishes there meanwhile is unowned and is "
                    "collected by the deferred pass",
                    investigation_id,
                )
            return
        # TTL and staging-drain lanes normally have no close marker; avoid a no-op heap update.
        await conn.execute(
            "UPDATE investigations SET rootfs_cleanup_pending_at = NULL "
            "WHERE id = %s AND rootfs_cleanup_pending_at IS NOT NULL",
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
    fault is different: it means SENSITIVE bytes are not being reclaimed, so the job fails and
    surfaces durably in the ``jobs`` table instead of only in a repeating log line.

    The first real fault **ends the loop** rather than attempting the remaining checksums. Pressing
    on through a store-wide refusal would multiply the store client's retry budget by the remaining
    worklist while occupying the worker. No owner lock is held during capture or exact deletion.
    An untouched checksum keeps its row for the next row-driven sweep; an exact-delete fault happens
    after its row commits retired, so the upload-version orphan sweep rediscovers every survivor.

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
    await _finish_drained_investigation(
        conn, investigation_id, rootfs_dir=rootfs_dir, uploads_dir=uploads_dir
    )
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
