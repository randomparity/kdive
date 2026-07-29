"""Worker handler for the internal ``reclaim_investigation_rootfs`` job (ADR-0442, #1522).

Reclaims an investigation's committed uploaded-rootfs bases: for each due ``artifacts`` row, the
ADR-0441 §6 pin gate and then the ADR-0495 in-flight-download gate, then the staged base unlink, the
object delete, and the row delete — in that order (ADR-0442 §4). The work runs on the worker rather
than the reconciler because the worker created the staging tree: on a host-process local-libvirt
deployment it runs as root while the reconciler runs as the invoking user, so a reconciler-side
unlink raises ``PermissionError`` after the object is already gone (#1522). Co-location is
structural here — the worker that claims a local-libvirt job is the libvirt host, the same
assumption ``provision``'s staging already makes — so the removed stat-based probe has nothing left
to answer.

Each checksum's gate-and-reclaim runs in one transaction under the ``INVESTIGATION`` advisory lock
that System bind holds transaction-scoped until its row commits, so a bind either is seen as a
pre-overlay referencer (pinning the base) or waits behind the reclaim. The *fetch* is not serialized
by that lock at all, which is why the second gate exists: it asks the kernel whether a download of
this base is in flight instead of inferring it from a System state column (ADR-0495).
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
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ReclaimInvestigationRootfsPayload, load_payload
from kdive.providers.shared.runtime_paths import (
    ROOTFS_DIR,
    STAGED_ROOTFS_MARKER_SUFFIX,
    UPLOADS_DIR,
    overlay_name,
    staged_rootfs_marker_path,
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
_INV_ROOTFS_KEYS_SQL = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'investigations' AND retention_class = 'rootfs' AND owner_id = %s"
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

    The **set** form rather than a per-token predicate is what the filesystem-keyed sweep needs
    (ADR-0494): that sweep starts from directory entries, not from a worklist of rows, so it must
    decide every token it finds against one enumeration of the System rows instead of re-running
    the referencer query per candidate under the same held lock.
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

    Expressed against :func:`pinned_rootfs_tokens` so the row-driven reclaim gate and the
    filesystem-keyed sweep cannot drift apart on what "pinned" means — the drift ADR-0494 §3 is
    about, since the sweep now unlinks bases the row-driven path never sees.
    """
    return token not in await pinned_rootfs_tokens(conn, investigation_id, rootfs_dir=rootfs_dir)


def _rootfs_token_from_key(object_key: str) -> str:
    """Extract the content-address token from a ``rootfs-<token>`` investigation object key."""
    return object_key.rsplit("/", 1)[-1].removeprefix("rootfs-")


def _unlink_staged_base(uploads_dir: str, investigation_id: UUID, token: str) -> None:
    """Unlink the staged base and its completion marker; ``ENOENT`` is the achieved post-state.

    Any **other** ``OSError`` propagates — from either unlink, which is why they share one region —
    so the caller defers the whole checksum before deleting the object or the row (ADR-0442 §4):
    neither SENSITIVE copy may be dropped while the local base survives.

    The marker (ADR-0451) goes **first**, which is the conservative order. Interrupted after it,
    this leaves "base without marker", which the reuse gate rejects and the next fetch re-stages:
    correct, at the cost of one download. The opposite order would leave "marker without the base it
    attests to" — harmless today only because the gate also requires the base to be a regular file,
    and there is no reason to create a state whose safety rests on a second condition holding.
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
    """Collect everything in an investigation's staging dir that nothing owns or pins.

    ``protected_tokens`` is the union of the tokens an ``artifacts`` row still owns and the tokens
    a live System pins (:func:`pinned_rootfs_tokens`), read by the caller under the
    ``INVESTIGATION`` lock. ``drained`` says whether **no** rootfs row remains, which is what
    licenses retiring the directory itself.

    Keying the collection on the token rather than on ``drained`` is ADR-0494's whole change
    (#1559). The precondition the ungated unlink used to rest on — "no rootfs row remains, so every
    file here is unowned" — is unreachable for the three states that leak: a checksum whose unlink
    or object delete faults permanently keeps its row and so kept the directory out of every sweep;
    a never-closed investigation whose rows all drained is selected by no row-keyed worklist at all;
    and a base published after this pass's own enumeration outlives the ``rmdir``. Deciding each
    file against the owned/pinned set instead makes the collection independent of all three.

    Args:
        uploads_dir: The staging root (``<uploads>/<investigation_id>/`` is swept).
        investigation_id: The investigation whose staging directory this is.
        protected_tokens: Tokens that must survive — owned by a row, or pinned by a live System.
        drained: Whether no rootfs row remains, licensing the ``rmdir``.

    Returns:
        Whether the drain must be **deferred** to a later pass rather than settled now. See
        :func:`_finish_drained_investigation` for what the caller does with it.

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
    it deliberately covers every token in the directory, not one base's. The partial glob needs no
    ``protected_tokens`` test of its own for the same reason: a partial is never a published base,
    so ownership says nothing about it and the ``flock`` is the whole answer.

    A staged **base** and its ADR-0451 completion marker are collected only when their token is
    outside ``protected_tokens`` (:func:`_unlink_unowned_base`,
    :func:`_unlink_completion_markers`). Before ADR-0494 both globs were unconditional and were
    licensed by ``drained``; testing the token instead is what lets them run while other rows
    survive, and it is also what keeps them from destroying a live base's marker — an unconditional
    marker glob under a surviving row would force a re-download of a perfectly good base, because
    the ADR-0451 reuse gate rejects a marker-less one.

    A pinned-but-unowned base is *left* and defers the drain. It is the one survivor whose cause is
    provably transient in the same sense a held ``flock`` is: the pin is a live System, and the
    reconciler drives every System to a terminal state, so a later pass collects it. Clearing the
    marker on it would retire the last collector for a SENSITIVE base, which is the leak this
    change exists to close.

    When the ``rmdir`` still fails after a collecting pass, the collection is re-run **once** and
    the ``rmdir`` retried (:func:`_drain_staging_dir`). That is the narrow window in which a doomed
    fetcher publishes between this pass's own globs and its ``rmdir``: the caller then clears the
    drain marker and nothing revisits the directory, so a single bounded re-pass is what converts
    that from a permanent leak into one extra ``readdir``. It is deliberately bounded at one rather
    than looped: an unbounded retry is the never-terminating drain ADR-0452 §5 rejected.

    ADR-0495 narrows the window rather than removing it, so the re-pass stays. A doomed fetcher that
    holds an ``flock`` on its partial now defers its checksum in :func:`_reclaim_one_checksum`,
    which keeps that row and turns this tail back at its ``drained`` test before either ``rmdir``.
    What is left is a fetcher that had not yet *created* its partial when the reclaim probed, and
    whose download then completes anyway because the object delete landed behind its reads — the
    timed-out delete ADR-0442 records as still landing later, or a store that serves a deleted key.
    Narrower than before and not empty.
    """
    inv_dir = Path(uploads_dir) / str(investigation_id)
    if not drained:
        # A rootfs row survives, so this investigation is still on the row-driven worklist and that
        # lane is the follow-up: nothing here retires the directory or the drain marker. The two
        # token-keyed collectors still run, because "this token has no row and no pin" is decided
        # per file and is as true here as it is on a full drain — that is the whole point of
        # ADR-0494 §1, and it is what reaches a base orphaned beside a permanently faulting row.
        #
        # The *partial* glob deliberately does not. Its candidates are decided by the ``flock``
        # gate, not by ownership, and a surviving row means a fetch of that base can legitimately be
        # in flight; ADR-0442 §7's "a remaining row's in-flight download is never clobbered" is
        # retained here unchanged. #1565 was **not** answered by widening it: ADR-0495 puts the
        # liveness probe in `_reclaim_one_checksum`, per token, where deferring retains the row the
        # TTL lane re-selects on. Widening it here would buy nothing that probe does not — this tail
        # unlinks partials, it cannot retain a row — while unlinking a live sibling's partial is
        # exactly what the row test above is protecting against.
        _collect_unowned_bases(inv_dir, protected_tokens)
        _unlink_completion_markers(inv_dir, protected_tokens)
        return True
    outcome = _collect_unprotected(inv_dir, protected_tokens)
    if outcome.held:
        return True
    return _drain_staging_dir(inv_dir, protected_tokens, left_pinned=outcome.left_pinned)


@dataclass(frozen=True, slots=True)
class _SweepOutcome:
    """What one collecting pass over a staging directory actually observed.

    Both fields report an **observation**, never a set the caller happens to hold: "a live writer
    holds a partial" and "a base whose token is protected is still sitting there" are the two
    conditions that explain a surviving directory, and each must be answered by the walk. Deriving
    either from ``protected_tokens`` being non-empty would state a conditional as an invariant — the
    defect ADR-0452 §4 removes from this same subsystem — and would report a base left behind for an
    investigation whose directory is empty, or absent.
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

    The re-pass exists for one reachable state and no other: a fetcher published a base (or its
    marker) after :func:`_collect_unprotected` had already enumerated the directory, so the
    ``rmdir`` finds a file that no glob this pass ever saw. The caller clears the drain marker on a
    ``False``, retiring every collector this investigation has, so without the re-pass that file is
    a permanent SENSITIVE leak — the shape #1559 is about.

    A re-pass that finds a **held** partial defers instead of warning: a writer arrived, which is
    the one outcome ADR-0452 §5 established is *provably* transient.

    A base left behind because a live System pins it does **not** defer. It is reported and the
    marker cleared, exactly as an unremovable directory is (ADR-0494 §3). Retaining on it looks
    right and is not: ``_ROOTFS_REFERENCERS_SQL`` excludes only ``torn_down``, ``failed`` is
    terminal with no transition out of it, and nothing removes a ``failed`` System's overlay —
    ``remove_overlay`` runs from teardown and ``_reclaim_materialized_on_failure`` only undoes an
    overlay its own call created. So a pin can be permanent, and pinning the drain marker on it
    resurrects the never-clearing marker and the re-fail-every-pass loop ADR-0442 was written about.
    The base is still never unlinked, which is the part that matters; what the marker would buy is
    only who revisits it, and for an ``open``/``active`` investigation that is
    ``sweep_unowned_investigation_rootfs_staging``, which is not marker-keyed.
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

    The token is the base's stem, the same derivation :func:`staged_rootfs_path` writes and
    :func:`_rootfs_token_from_key` reads back off the object key — so a base a surviving row owns
    is skipped by name equality rather than by any inference about the investigation's state. That
    equality rests on a content-address token never containing a ``.``
    (:func:`kdive.artifacts.content_address.rootfs_object_token` emits unpadded base64url, whose
    alphabet is ``[A-Za-z0-9_-]``); a dotted token would make ``Path.stem`` drop a segment and leave
    every base *unprotected*, so the property is pinned by a test rather than assumed here.

    Returns:
        Whether a base was **left** because its token is protected. Reported as an observation of
        the walk rather than derived from ``protected_tokens`` being non-empty: the ordinary case is
        that the row-driven reclaim already unlinked the base as its row drained, so a non-empty set
        with an empty directory is the norm, not the exception.
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
    """Collect every ADR-0451 completion marker whose token nothing owns or pins.

    Without this the ``rmdir`` below fails ``ENOTEMPTY`` on **every** drained investigation that
    ever staged a base, leaking one directory apiece — and, since ADR-0452 §7, doing it loudly: the
    unexplained-survivor ``WARNING`` would fire on every ordinary drain and train an operator to
    ignore the one line that reports a genuinely unreadable staging tree.

    Not ``flock``-gated, per :func:`sweep_investigation_staging_dir`; **is** token-gated, which the
    unconditional version could not be. ADR-0452 licensed the ungated glob on the sweep running
    only once no rootfs row remained, so no marker it could reach attested to a live base. Once the
    sweep runs alongside surviving rows (ADR-0494 §3) that stops holding: a marker unlinked out from
    under a base its row still owns makes the ADR-0451 reuse gate reject that base and re-download
    it — silently, since the re-stage succeeds.

    A successful collection is deliberately silent: :func:`_unlink_unowned_base` already
    ``WARNING``\\ s for the publish-after-reclaim condition that is the only way either file
    survives to here, and a marker beside a collected base would only double that line. A
    per-candidate unlink *fault* is logged, because this pass is the last collector, and the walk is
    logged for ADR-0452 §7's reason: ``Path.glob`` yields nothing for a directory it cannot
    enumerate rather than raising.
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

    This ``rmdir`` is the only step that can tell a directory that **drained** from one this pass
    could not *read*: ``Path.glob`` yields nothing for an unenumerable directory instead of raising,
    so every walk above returns quietly and ``held`` comes back ``False`` either way. Left as a bare
    ``suppress(OSError)`` that would be the one silent skip in the whole design — and the most
    consequential, since the caller then clears ``rootfs_cleanup_pending_at`` and retires every
    collector this investigation has for a tree that may hold SENSITIVE bytes up to the 50 GiB
    canonical cap. The triggers are this subsystem's own: the worker/staging-user uid asymmetry
    ADR-0442 documents and #1522 was bitten by, a mode change on the staging tree, ``EIO``, a stale
    NFS handle.

    ``ENOENT`` is the achieved post-state for an investigation that never staged anything, so it
    reads as success. Everything else is returned rather than logged here, because the caller runs
    one more collecting pass before deciding whether the survivor is a surprise
    (:func:`_drain_staging_dir`).
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

    ADR-0452 §5's argument that a permanent fault must not pin the drain marker forever applies to a
    permanently unremovable directory exactly as it does to a permanently unopenable partial, so the
    marker is cleared regardless and this line is what an operator gets instead.
    """
    _log.warning(
        "the rootfs staging directory %s survived its investigation's drain (%s) and no live "
        "writer explains it; its drain marker is cleared regardless, so nothing will revisit "
        "it — inspect it for uncollected SENSITIVE staging files",
        inv_dir,
        reason,
    )


def _unlink_unowned_base(base: Path) -> None:
    """Collect a staged base nothing owns or pins (ADR-0452 §6, ADR-0494 §3, #1559).

    The caller has just read, under the ``INVESTIGATION`` lock, both the tokens an ``artifacts``
    row still owns and the tokens a live System pins, and ``base``'s token is in neither
    (:func:`_collect_unowned_bases`). That per-token test is the licence for the unlink, and it is
    what replaced ADR-0452's zero-row precondition — do not move this call anywhere those two sets
    have not just been read under the lock.

    **What "unowned" does and does not cover.** No overlay can be backed by this base: an overlay
    present for a referencing System puts its token in the pinned set (:func:`_overlay_pins_base`),
    and this is skipped. That closes the residue ADR-0452 §6 recorded — an overlay created *after*
    the row was reclaimed, by the doomed fetcher whose partial an earlier pass skipped — which the
    zero-row precondition could not see, because that precondition read rows and the overlay is a
    file. What remains conditional is the instant between the pin read and the unlink. ADR-0495
    narrows the publish-after-reclaim shape that produces such a System — a fetcher holding its
    partial's ``flock`` now defers the whole checksum, so its row is never reclaimed under it — down
    to a fetcher that had not created its partial yet when the reclaim probed and whose download
    outlived the object delete anyway. #1558's remaining half is the classifier itself, which still
    cannot tell that a ``torn_down`` System was mid-provision.

    It should also rarely fire. :func:`_unlink_staged_base` removes each base as its own row drains,
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


def _live_writer_holds_a_partial(uploads_dir: str, investigation_id: UUID, token: str) -> bool:
    """Whether a live writer still ``flock``\\ s one of **this** checksum's staging partials.

    The row-driven reclaim's own liveness question (ADR-0495, #1565/#1558). The ADR-0441 §6 pin gate
    answers it from the referencing System's state column plus overlay presence, and that classifier
    is falsified by the two transitions that drop the pin while the download continues:
    :data:`_ROOTFS_REFERENCERS_SQL` excludes ``torn_down`` outright, ``failed`` is outside
    :data:`_PRE_OVERLAY_STATE_VALUES`, and a ``provisioning`` System has no overlay file yet. So the
    same question ADR-0446 and ADR-0452 both ended up asking the *kernel* is asked here too, one
    step ahead of the first unlink, rather than re-derived from a state column a third time.

    Scoped to ``<token>.*.partial`` — the same glob the fetch-side sweep uses against its own
    ``dest`` — not to every partial in the directory. The staging tree is per *investigation* and
    content-addressed within it, so a sibling System downloading a different base is a routine
    concurrent state; deferring this checksum on that file would stall it for the length of an
    unrelated multi-GiB download and re-create ADR-0442 §7's starvation keyed on an unrelated name.

    An **unheld** candidate of this token is a crash orphan and is unlinked in passing, through the
    shared reclaim-side gate rather than a second probe, so a dead fetcher's partial cannot defer
    the checksum forever — which would leak the base, the object and the row for as long as it sat
    there, re-creating #1565's unbounded leak inside its own fix. ``unlink_when_unlockable=True``
    matches the drain tail rather than the fetch side: this is a reclaim-side collector, and on a
    host that cannot ``flock`` at all the writer staged unguarded, so a skip protects nothing.

    A staging directory this process cannot *enumerate* reads as "no live writer", because
    ``Path.glob`` yields nothing for it instead of raising. That is not a fail-open hazard: the very
    next step is :func:`_unlink_staged_base`, which needs write on that same directory and defers
    the checksum on any ``OSError`` but ``ENOENT``, so nothing is deleted either way. Recorded in
    ADR-0495 rather than guarded, for the reason ADR-0494 records the same shape.
    """
    dest = staged_rootfs_path(investigation_id, token, upload_dir=Path(uploads_dir))
    held = False
    for partial in dest.parent.glob(f"{dest.stem}.*.partial"):
        if unlink_partial_if_unheld(partial, unlink_when_unlockable=True):
            held = True
    return held


async def _reclaim_one_checksum(
    conn: AsyncConnection,
    store: ArtifactObjectDeleter,
    *,
    artifact_id: UUID,
    investigation_id: UUID,
    rootfs_dir: str,
    uploads_dir: str,
) -> bool | None:
    """Gate and reclaim one due row under the investigation lock (ADR-0442 §3/§4, ADR-0495).

    Returns ``True`` when the checksum drained, ``False`` on a real fault (the caller fails the job
    once every other checksum has been attempted), and ``None`` when there was nothing to do — the
    row already drained, the gate pins the base, or a live writer holds one of this token's staging
    partials. All three are expected steady states, not errors. Order is staged base -> object ->
    row, so a fault leaves the re-downloadable copy rather than an unreclaimable local base, and the
    row (the worklist anchor) outlives both.

    **Two gates, in this order, and neither can stand in for the other.** The ADR-0441 §6 pin gate
    asks whether a System's *overlay* is backed by this base; it reads rows and one ``stat``, and it
    is the cheaper question, so it runs first. :func:`_live_writer_holds_a_partial` asks whether a
    *download* of this base is in flight — which the pin gate provably cannot answer, because
    ``PROVISIONING -> TORN_DOWN`` and ``PROVISIONING -> FAILED`` both drop the pin while the
    detached, uncancellable ``asyncio.to_thread`` download keeps writing, and nothing serializes the
    two (the fetch takes only its per-(investigation, checksum) session lock, never the
    ``INVESTIGATION`` lock this holds).

    Deferring on a held partial keeps **all three** copies: the row is not deleted, so the
    investigation stays on whichever row-driven worklist selected it and the very next pass retries
    (ADR-0495 §2) — no marker, no new lane, no ``systems.created_at`` age gate in the way. It is
    also what stops the object delete from turning a live ranged-GET download into 404s that surface
    as an ``INFRASTRUCTURE_FAILURE`` "failed to stage" pointing an operator at the object store.

    The residual window is the instant between the probe and the base unlink, plus a fetch that has
    resolved its ``artifacts`` row but not yet created its partial — it waits on the fetch session
    lock in between, so that window is not short. ADR-0495 states it rather than claiming closure.
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
        if await asyncio.to_thread(
            _live_writer_holds_a_partial, uploads_dir, investigation_id, token
        ):
            # The observation is logged one frame down, on the file; this line is the *decision*,
            # and it is the only evidence the reclaim fired at all — the job succeeds either way, so
            # a silent return is indistinguishable from an already-drained row. Worded for what is
            # retained, because that is what an operator needs to reconcile against the object
            # store.
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


async def _owned_rootfs_tokens(conn: AsyncConnection, investigation_id: UUID) -> frozenset[str]:
    """The content-address tokens ``investigation_id``'s surviving rootfs ``artifacts`` rows own."""
    async with conn.cursor() as cur:
        await cur.execute(_INV_ROOTFS_KEYS_SQL, (investigation_id,))
        return frozenset(_rootfs_token_from_key(str(row[0])) for row in await cur.fetchall())


async def _finish_drained_investigation(
    conn: AsyncConnection, investigation_id: UUID, *, rootfs_dir: str, uploads_dir: str
) -> None:
    """Sweep the staging dir, then clear the close marker if it settled (ADR-0442 §7, ADR-0494).

    Reads the real post-state rather than trusting the reclaim loop's own single-pass flags, which
    is what makes the bookkeeping correct across a worker that died mid-reclaim, a stale due-set,
    and a concurrent finalize. ADR-0442 read that post-state as one bit ("are there rows left?") and
    ran the staging sweep only when the answer was no. ADR-0494 reads it as two **sets** — the
    tokens rows own, and the tokens live Systems pin — and always runs the sweep, because the
    zero-row form starved exactly the case that leaks: a checksum whose unlink or object delete
    faults permanently keeps its row, so its investigation's staging directory was never swept
    again and a base orphaned beside it was collected by nothing (#1559).

    Clearing is still keyed on the drain rather than on which sweep enqueued the job — a TTL job
    only runs against an ``open``/``active`` investigation, whose marker is already NULL.

    The marker is retained whenever the sweep defers, and the sweep defers only on causes that are
    provably transient (ADR-0452 §4, ADR-0494 §4): a rootfs row survives, a live writer still
    ``flock``\\ s a partial, or a live System pins an otherwise-unowned base. Neither neighbouring
    choice is right. Clearing unconditionally would leave that partial with **no** collector — this
    marker is the only thing that re-enqueues a reclaim for a closed investigation, and the
    fetch-side opportunistic sweep only fires on the next fetch of that base, which never comes once
    the investigation is closed — so a holder killed mid-download would leak its multi-GiB SENSITIVE
    partial permanently. Retaining on *every* non-drain would be worse the other way: an unopenable
    or unlinkable partial, or an unremovable directory, is permanent until an operator acts, and
    pinning the marker on those resurrects the never-clearing marker and the re-fail-every-pass loop
    ADR-0442 was written about.

    **The close-driven lane is no longer the only one that re-runs.** A TTL job runs against an
    ``open``/``active`` investigation whose marker is already NULL, so retaining is a no-op there,
    and that lane's worklist (``reconciler.cleanup.gc._TTL_ROOTFS_OBJECTS_SQL``) is a pure
    ``artifacts`` query over rows this job just deleted. ADR-0494 adds
    ``sweep_unowned_investigation_rootfs_staging``, whose worklist is the ``systems`` rows that
    reference an uploaded base rather than the ``artifacts`` rows that own one, so a never-closed
    investigation whose rows have all drained keeps being revisited.

    **The rows-still-present half is answered in the row-driven path, not here** (ADR-0495, #1565).
    A live-held partial for a checksum still under reclaim now defers that checksum inside
    :func:`_reclaim_one_checksum`, which keeps its ``artifacts`` row — and that row is exactly what
    ``reconciler.cleanup.gc._TTL_ROOTFS_OBJECTS_SQL`` selects on, so the retry is the existing lane
    re-selecting the same investigation on its next pass. Nothing was added here: the deferral this
    tail decides on is still only about the *staging directory*, and the close marker is
    deliberately not overloaded onto an open investigation, because it is durable state whose
    meaning is "this investigation was closed and its rootfs is being reclaimed".

    What remains unreachable by every lane is an ``abandoned`` investigation (ADR-0494's
    Consequences). No writer transitions one to that state today, so it is named rather than
    guarded."""
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, investigation_id),
    ):
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
                # WARNING, not INFO, and worded for the *observation* and the achieved post-state
                # rather than for the UPDATE below — which is a no-op on the TTL path, whose
                # investigations carry a NULL marker, so "the marker is kept" would be false exactly
                # there. Suppressed while a row survives: the row lane is the follow-up there, and
                # warning on every pass of the whole grace window would bury the line that reports a
                # genuine survivor.
                _log.warning(
                    "investigation %s has no rootfs rows left, but a live writer still holds a "
                    "staging partial; keeping its staging directory and deferring the drain until "
                    "that writer exits. Anything it publishes there meanwhile is unowned and is "
                    "collected by the deferred pass",
                    investigation_id,
                )
            return
        # Predicated on the column, so the settle path does not write a new heap tuple on every pass
        # for an investigation whose marker is already NULL — which is *every* pass of the TTL and
        # staging-drain lanes, both of which run against `open`/`active` investigations that never
        # had one set. Without the predicate this is one dead tuple per investigation per pass,
        # forever, on a table read by every bind.
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
