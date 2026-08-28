"""Stream, decode, capacity-check, and verify uploaded rootfs bytes."""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

# The function rather than the module, so a test's monkeypatch is scoped to this module instead of
# replacing ``shutil.disk_usage`` process-wide for every other importer for the test's duration.
from shutil import disk_usage
from uuid import UUID, uuid4

from kdive.artifacts.uploads.transport_encoding import (
    GZIP_ENCODING,
    TRANSPORT_CHECKSUM_GATE,
    StripDecodeRequest,
    normalize_encoding,
    strip_gzip_to_writer,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.upload_contracts import UploadObjectStore
from kdive.providers.local_libvirt.lifecycle.rootfs.upload_publication import (
    _discard,
    _durable_replace,
    _require_qcow2_magic,
    _sibling_already_published,
    _staging_fault,
)
from kdive.providers.shared.staging.staging_partials import unlink_partial_if_unheld

_log = logging.getLogger(__name__)

_TENANT = "local"
_OWNER_KIND = "investigations"
# The qcow2 magic every canonical rootfs base must start with (bytes ``51 46 49 fb``); a base that
# does not is rejected here rather than failing late and confusingly at ``qemu-img`` (ADR-0438).
_QCOW2_MAGIC = b"QFI\xfb"
# The identity path's per-read window: the staging loop hashes and writes one chunk at a time, so
# peak memory is this constant rather than the object size (up to the 5 GiB single-PUT ceiling).
# Matches the gzip path's ranged-read window in ``transport_encoding``.
_STREAM_CHUNK_BYTES = 4 * 1024 * 1024
# The floor the staging free-space precheck keeps free beyond the base itself (ADR-0450, #1525).
# Fixed rather than a fraction of the object: a percentage scales with the base, so 10% of the
# 50 GiB canonical cap would demand 5 GiB of slack and refuse a stage on a volume that comfortably
# holds it. It is also a property of the *volume*, not of the base — capping it at the base size
# would let a stream of small bases walk the volume to zero one step at a time, which is the same
# harm arriving slower. The floor is what stops a *passing* check from meaning "this write ends the
# volume at exactly zero bytes free", which is the degraded state for the sibling overlays the
# precheck exists to protect, not the avoidance of it. A consequence worth knowing before tuning
# it: a volume with less than this free refuses every uploaded-rootfs stage regardless of base
# size. It is a judgment call sized to keep a volume usable, not a measured overlay growth rate,
# and it is not a reservation — see :func:`_require_staging_free_space`.
_STAGING_FREE_SPACE_MARGIN_BYTES = 1024**3

if ctypes.sizeof(ctypes.c_long) != 8:
    raise RuntimeError("local-libvirt rootfs staging requires a 64-bit native off_t")

_libc = ctypes.CDLL(None, use_errno=True)
_fallocate = _libc.fallocate
_fallocate.restype = ctypes.c_int
_fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]


def _native_fallocate(fd: int, length: int) -> None:
    """Allocate ``length`` bytes with Linux ``fallocate(2)``, preserving native errno."""
    ctypes.set_errno(0)
    if _fallocate(fd, 0, 0, length) == 0:
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number))


def _reserve_staging_space(
    fd: int,
    *,
    partial: Path,
    dest: Path,
    budget: _StagingBudget | None,
    system_id: UUID,
) -> None:
    """Reserve the base's blocks atomically, or degrade when native allocation is unsupported."""
    if budget is None:
        return
    try:
        _native_fallocate(fd, budget.required)
    except OSError as error:
        if error.errno in {errno.ENOSYS, errno.EOPNOTSUPP}:
            _log.warning(
                "the filesystem holding %s does not support native rootfs staging reservations "
                "(%s); continuing with only the advisory free-space precheck, so concurrent "
                "different-base stages can still overcommit this volume",
                partial,
                error.strerror,
            )
            return
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise CategorizedError(
                f"could not reserve {budget.required} bytes for the uploaded rootfs at "
                f"{str(dest)!r} ({error.strerror}); free capacity on that filesystem and "
                "re-issue the provision",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "system_id": str(system_id),
                    "dest": str(dest),
                    "requested_bytes": budget.required,
                    "budget_source": budget.source,
                    "errno": error.errno,
                },
            ) from error
        raise


def _unlink_orphan_partials(dest: Path) -> None:
    """Opportunistically unlink unheld ``<token>.*.partial`` crash orphans (ADR-0446)."""
    with suppress(OSError):
        for orphan in dest.parent.glob(f"{dest.stem}.*.partial"):
            unlink_partial_if_unheld(orphan, unlink_when_unlockable=False)


@contextmanager
def _flocked_partial(partial: Path) -> Iterator[int]:
    """Create this fetcher's partial and hold an exclusive ``flock`` on it while it stages.

    The liveness marker :func:`_unlink_orphan_partials` reads. Two *writers* never contend, because
    partial names carry a ``uuid4``, so no two fetchers ever name the same file; the only contender
    is a sibling's sweep, handled below. The descriptor is held across the download, the verify, and
    :func:`_durable_replace`, which is exactly the window in which a sweep must not touch the file.

    ``fcntl.flock`` is BSD ``flock(2)`` and that is load-bearing: the lock belongs to the open file
    **description**, so it survives each stager's separate ``partial.open("wb")`` handle,
    :func:`_require_qcow2_magic`'s ``"rb"`` handle, and :func:`_fsync_path`'s third descriptor all
    being opened and closed on the same inode underneath it. POSIX record locks
    (``fcntl.lockf`` / ``F_SETLK``) have the opposite rule — closing *any* descriptor on the file
    drops the process's locks — so swapping to them in the name of a "more portable" API would
    silently unprotect the whole verify-and-publish window. ``test_the_partial_stays_locked_after
    _the_stagers_writer_closes`` fails if that swap is ever made.

    The mode is ``0o666`` so umask application matches the ``partial.open("wb")`` this fronts, byte
    for byte. That is load-bearing rather than tidy: ``os.replace`` carries the partial's mode onto
    the published base, and QEMU reads that base as the unprivileged hypervisor user, so tightening
    it to ``0o600`` — the reflex for a SENSITIVE file — would make every base staged after this
    unreadable to the hypervisor. ``O_EXCL`` because a file already sitting at a ``uuid4`` path is
    not something to write through silently.

    ``open`` and ``flock`` are two syscalls, so a sibling's sweep can win the gap between them
    (ADR-0446 §3). It has two interleavings and they end the same way: if the sweep already unlinked
    and closed, this ``flock`` succeeds and ``st_nlink`` is zero; if the sweep still holds its own
    lock, this ``flock`` raises ``EWOULDBLOCK`` and the sweep is about to unlink. Retrying the
    second buys nothing — the sweep's very next syscall removes the file the retry would win — so
    both raise one attributable error instead, rather than a bare ``EWOULDBLOCK`` that
    :func:`_staging_fault` would render as "failed to stage", pointing an operator at the object
    store over a local race.

    Raises:
        OSError: ``ENOENT`` if a concurrent orphan sweep won the create-then-lock window.
    """
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise _swept_partial_error(partial, window=_CREATION_WINDOW) from err
        except OSError as err:
            # A filesystem that cannot lock at all (``ENOLCK`` on an NFS mount whose lock manager is
            # down, ``EOPNOTSUPP`` on some FUSE and 9p backends). Staging unguarded is exactly the
            # pre-ADR-0446 behavior and no worse: a sweep on this same filesystem cannot lock
            # either, so it skips every candidate. Failing here instead would turn a filesystem
            # without lock support into a total uploaded-rootfs outage, and would do it with a
            # "failed to stage" message pointing at the object store.
            _log.warning(
                "this host cannot flock the staging partial %s (%s); staging it unguarded — a "
                "sibling's orphan sweep on this filesystem cannot lock either, so it skips every "
                "candidate and collection falls to the investigation-reclaim sweep",
                partial,
                err.strerror,
            )
        _require_still_linked(fd, partial, window=_CREATION_WINDOW)
        yield fd
    finally:
        os.close(fd)


def _require_still_linked(fd: int, partial: Path, *, window: str) -> None:
    """Raise ``ENOENT`` with the affected window when a sweep unlinks the partial."""
    if os.fstat(fd).st_nlink == 0:
        raise _swept_partial_error(partial, window=window)


#: What took the partial, per call site. The two are materially different conditions and an operator
#: acts on them differently, so the fault names the one that applies rather than one fixed string:
#: the creation window is a sub-millisecond race, while the download window points at an unguarded
#: stage. Both sweeps are ``flock``-gated since ADR-0452, so a *guarded* stage is no longer taken by
#: a sweep at all — but the message states the observation and names its likely cause rather than
#: asserting one, which is the same principle ADR-0452 §3 applies to the sweep's own WARNING.
_CREATION_WINDOW = "between its creation and its lock"
_DOWNLOAD_WINDOW = (
    "while it was being downloaded; the usual cause is a stage the filesystem could not flock "
    "(ADR-0446 §5), on which neither sweep's gate exists — but a lock dropped by lock-manager "
    "recovery, or anything outside kdive removing the file, leaves the same state"
)


def _swept_partial_error(partial: Path, *, window: str) -> OSError:
    """The ``ENOENT`` for a partial a concurrent sweep took, naming which window it went in."""
    return OSError(
        errno.ENOENT,
        f"a concurrent orphan sweep took the staging partial {window}; retry the provision",
        str(partial),
    )


@dataclass(frozen=True, slots=True)
class _StagingBudget:
    """The bytes a stage will occupy, and where that figure came from.

    The provenance is load-bearing rather than diagnostic colour. On the identity path the figure is
    the stored object's **exact** size. On the gzip path it is the agent's declared
    ``uncompressed_size``, which nothing validates against the real decompressed length: ADR-0437's
    validator enforces only "positive integer, under the 50 GiB cap", ``strip_gzip_to_writer``
    accepts less than the bound without complaint, and the agent-facing schema calls the field "the
    gzip-bomb bound" — language that positively invites rounding it up, because over-stating a bomb
    bound is the safe direction everywhere else in the system. Over-reserving is free for a
    *reservation* and is not free for a *refusal*, so an over-stated declaration must not be
    reported as a host that is out of disk with no hint of where the number came from (ADR-0450 §2).
    """

    required: int
    source: str


#: The provenance of a :class:`_StagingBudget`, and the ``budget_source`` detail it surfaces.
_OBJECT_SIZE = "object_size"
_UNCOMPRESSED_SIZE_BOUND = "uncompressed_size_bound"

#: Which of the two conditions refused a stage. Slugs rather than only prose because the structured
#: ``details`` reach a dashboard or a triage script that never sees the message, and "needs N, has
#: M" alone re-derives exactly the misattribution the split message exists to prevent.
_BASE_DOES_NOT_FIT = "base_does_not_fit"
_FLOOR_BREACHED = "floor_breached"
_SHORTFALL_PROSE = {
    _BASE_DOES_NOT_FIT: "the base does not fit at all",
    _FLOOR_BREACHED: "the base itself fits; it is the floor that would be breached",
}


def _staging_budget(
    effective: str | None, *, object_size: int, uncompressed_size: int | None
) -> _StagingBudget | None:
    """The staged base's budget, or ``None`` when the occupied size is not knowable up front.

    Identity stages the stored object verbatim, so its size is exact. The gzip path never writes the
    stored object at all — it is read through ranged GETs — and what lands on disk is the
    *decompressed* output, which the stored size understates by the whole compression ratio. Its
    budget is therefore ``uncompressed_size``, the declared upper bound on that output; budgeting
    the stored size instead would under-reserve by exactly the compression ratio. What that bound
    costs when it is over-stated is :class:`_StagingBudget`'s subject.

    A gzip declaration carrying no ``uncompressed_size`` has no knowable requirement, and returns
    ``None`` rather than falling back to the stored size — that fallback would under-reserve by
    construction, which is the exact failure the precheck exists to prevent. :func:`_stage_gzip`
    then rejects the declaration with the actionable ``CONFIGURATION_ERROR``, which is the error an
    agent can act on; a free-space message computed from a number nobody knows would bury it.
    ``None`` is likewise returned for an unsupported codec, whose own rejection is the right one.
    """
    if effective is None:
        return _StagingBudget(object_size, _OBJECT_SIZE)
    if effective == GZIP_ENCODING and uncompressed_size is not None:
        return _StagingBudget(uncompressed_size, _UNCOMPRESSED_SIZE_BOUND)
    return None


def _require_staging_free_space(
    dest: Path, *, budget: _StagingBudget | None, system_id: UUID
) -> None:
    """Refuse a stage the staging filesystem plainly cannot hold, before the first byte (ADR-0450).

    Since the identity path started streaming (#1520) a **rejected** object — a failed checksum, a
    non-qcow2 base — is written in full and only then rejected. ``UPLOADS_DIR`` and ``ROOTFS_DIR``
    are hardcoded siblings under ``/var/lib/kdive`` with no provider-side knob to relocate either,
    so unless an operator separately mounts one of them, those discarded bytes land on the same
    filesystem as every live System's qcow2 overlay: an oversized upload degrades *running guests*
    rather than only failing its own provision. This measures ``dest.parent``'s own filesystem, so
    it is correct under either mount layout — only the blast radius differs.

    **What this does not cover.** It is advisory, not a reservation, and one case is worth naming
    outright rather than leaving a reader to derive it: #1525's own worked example — two Systems in
    different investigations staging 4 GiB bases onto a volume with 6 GiB free — is **not**
    prevented. The fetch lock is per-(investigation, checksum), so nothing serializes them, and
    each passes its own check against the same free bytes before either writes. Free space can also
    vanish between this ``statvfs`` and the write, and a live guest's overlay keeps growing
    throughout. What the check buys is the single-stager case: one oversized or invalid object
    against a volume that was never going to hold it now fails immediately and attributably instead
    of after a multi-GiB write that takes the volume down with it. ADR-0450 §4 records the
    kernel-enforced alternative (``posix_fallocate`` on the partial) and why it is not taken here;
    #1546 tracks it. The real ENOSPC guard therefore remains :func:`_staging_fault`, and this shares
    its ``INFRASTRUCTURE_FAILURE`` category deliberately: splitting them would make an agent's
    handling of one physical condition depend on which side of a race window it was observed from.

    ``budget`` is ``None`` when the staged size is not knowable (see
    :func:`_staging_budget`); there is nothing to compare against, so the check is skipped
    rather than guessed at, and the codec's own declaration error carries the failure.

    The measured figure is ``statvfs``'s ``f_bavail`` — space available to unprivileged users,
    which is ``df``'s ``Avail`` column and excludes the filesystem's reserved blocks. The staging
    worker often runs as root and could write into that reserve, so this is deliberately the
    conservative number: those blocks exist to keep a full volume usable, which is the same thing
    this guard protects. The error text says which figure it is, so it reconciles with ``df``.

    A ``statvfs`` that *itself* faults degrades to staging with a warning rather than failing —
    ``EACCES`` under the worker/staging-user asymmetry ADR-0442 documents in this same subsystem,
    a transient ``EIO``. This is :func:`_flocked_partial`'s ``ENOLCK`` precedent: turning a host
    quirk that only disables an *advisory* check into a total uploaded-rootfs outage costs
    availability for no safety, since the write stays guarded by the real ENOSPC either way.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` naming the required and available bytes.
            ``CONFIGURATION_ERROR`` would be wrong: the upload is fine, the host is full, and a
            non-retryable category would tell the agent to re-declare something correct. One
            consequence of failing *fast* on a retryable category: ``jobs.fail`` requeues with no
            backoff, so a provision job now spends its three attempts in milliseconds and
            dead-letters, where the pre-precheck ENOSPC spread them over three multi-GiB downloads.
            The remedy is unchanged — free space, re-issue the provision — but it is re-issued
            rather than picked up by a later attempt. Not marked ``terminal``: a sibling stage
            finishing or the reclaim sweep can free space between attempts, so a retry is not
            provably useless.
    """
    if budget is None:
        return
    try:
        free = disk_usage(dest.parent).free
    except OSError as err:
        _log.warning(
            "could not measure the free space on the rootfs staging filesystem at %s (%s); staging "
            "without the precheck, so an oversized base is caught mid-write by ENOSPC instead — "
            "the behavior before the precheck existed",
            dest.parent,
            err.strerror,
        )
        return
    needed = budget.required + _STAGING_FREE_SPACE_MARGIN_BYTES
    if free >= needed:
        return
    reason = _BASE_DOES_NOT_FIT if free < budget.required else _FLOOR_BREACHED
    # Logged as well as raised, unlike every other refusal in this module, because this one's whole
    # remedy is host-side. The raised error reaches the operator only as one more
    # `INFRASTRUCTURE_FAILURE` among many in `record_job_failure`, so without this an operator
    # watching a filling volume cannot tell "provisions are being refused for capacity" from any
    # other infrastructure fault without pulling each job's failure_context back through MCP.
    _log.warning(
        "refusing to stage an uploaded rootfs under %s: it needs %d bytes free (%d for the base, "
        "from %s, plus a %d-byte floor) and only %d are available — %s",
        dest.parent,
        needed,
        budget.required,
        budget.source,
        _STAGING_FREE_SPACE_MARGIN_BYTES,
        free,
        reason,
    )
    raise CategorizedError(
        _shortfall_message(dest, budget=budget, free=free, needed=needed, reason=reason),
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={
            "system_id": str(system_id),
            "dest": str(dest),
            "needed_bytes": needed,
            "base_bytes": budget.required,
            "floor_bytes": _STAGING_FREE_SPACE_MARGIN_BYTES,
            "free_bytes": free,
            "budget_source": budget.source,
            "reason": reason,
        },
    )


def _shortfall_message(
    dest: Path, *, budget: _StagingBudget, free: int, needed: int, reason: str
) -> str:
    """The refusal text, naming which condition fired and where each of its two figures came from.

    The condition is reported because the fixed floor means most refusals are *not* "your object is
    too big": stage a 20 MiB base onto a volume with 900 MiB free and a message claiming the write
    would fill the volume is simply false — it would leave 880 MiB — while pointing the operator at
    the upload instead of at the volume-wide floor that actually fired.

    The base figure's provenance is reported on the gzip path for the reason :class:`_StagingBudget`
    gives: nothing checks the declared bound against the real decompressed size, so an agent that
    rounded it up is refused as though the host were full. Without this sentence that refusal names
    a host-side remedy for a declaration fault and the real cause is surfaced nowhere.
    """
    provenance = (
        " That base figure is the declared uncompressed_size upper bound rather than a measured "
        "size; if it was rounded up, re-declare the upload with the real decompressed size."
        if budget.source == _UNCOMPRESSED_SIZE_BOUND
        else ""
    )
    return (
        f"not enough free space to stage the uploaded rootfs at {str(dest)!r}: staging needs "
        f"{needed} bytes free ({budget.required} for the base, plus a "
        f"{_STAGING_FREE_SPACE_MARGIN_BYTES}-byte floor kdive keeps for the running Systems whose "
        f"overlays share this volume) and only {free} are available — {_SHORTFALL_PROSE[reason]}. "
        f"The available figure is the unprivileged-available space on the filesystem holding "
        f"{str(dest.parent)!r} (df's Avail column, which excludes the reserved blocks); free space "
        f"there and re-issue the provision.{provenance}"
    )


def stage_uploaded_rootfs(
    store: UploadObjectStore,
    *,
    object_key: str,
    dest: Path,
    encoding: str | None,
    uncompressed_size: int | None,
    system_id: UUID,
) -> None:
    """Download + verify the object and stage it to ``dest`` via a unique per-fetcher ``.partial``.

    HEAD the object (absent → ``CONFIGURATION_ERROR``, naming a staged base at ``dest`` when one is
    present — #1571; no stored checksum → ``INFRASTRUCTURE_FAILURE``), then refuse the stage
    outright if the staging filesystem cannot hold the base plus a margin
    (:func:`_require_staging_free_space`, #1525) — an advisory check, ordered behind the HEAD
    because the size it budgets comes from there. When ``encoding`` is ``gzip`` the object is
    streamed-decompressed
    (bounded by ``uncompressed_size``, gzip-bomb guarded, transport-hash verified); otherwise it is
    streamed verbatim and its SHA-256 verified. Neither path buffers the whole object. Either way
    the canonical base is qcow2-magic-validated and written atomically **and durably** (a
    ``<token>.<uuid>.partial`` temp, ``fsync``\\ ed, then ``os.replace``\\ d and the directory
    ``fsync``\\ ed — ADR-0443) so ``dest`` is only ever a verified base, survives a host crash, and
    two concurrent fetchers never share a partial. The partial is held under an exclusive ``flock``
    for its whole life (ADR-0446), so a sibling's orphan sweep cannot unlink it mid-download. It is
    unlinked in a ``finally``, so no failure — typed or not — leaves one behind.

    The publish is skipped when a sibling already published the same content-addressed base while
    this download ran (:func:`_sibling_already_published`), so a fetcher that lost the fetch lock
    mid-transfer does not swap the inode out from under a guest already booting off it.
    """
    head = store.head(object_key)
    if head is None:
        try:
            base_present = stat.S_ISREG(dest.stat().st_mode)
        except OSError:
            # Absent, an absent parent, or unreadable — every case degrades to the plain
            # never-uploaded message below rather than risking a false "a base is present" for a
            # path this call cannot actually confirm holds one.
            base_present = False
        if base_present:
            # ADR-0451 rejects every pre-marker base once, which routes a provision with a good
            # local base through this HEAD for the first time — before it, a present magic-passing
            # base short-circuited the caller and this branch was unreachable with anything staged.
            # If the object is also gone (an out-of-band delete, a lifecycle rule, a partial
            # restore), the plain "never uploaded" text sends an operator to re-upload something
            # that visibly already exists at `dest`. Naming that base is not a remedy in itself —
            # ADR-0451 rejects adopting a local base with no way to verify it against the missing
            # object, for the same reason it rejects adopting a marker-less one — so this only
            # describes the condition and points at the object as what needs restoring.
            raise CategorizedError(
                f"the uploaded rootfs object is gone from the object store, but a staged base is "
                f"already present at {str(dest)!r} with no object left to verify it against; it is "
                "not reused unverified — restore the object or re-upload it",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "dest": str(dest)},
            )
        raise CategorizedError(
            "upload-kind rootfs was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id)},
        )
    if head.checksum_sha256 is None:
        raise CategorizedError(
            "uploaded rootfs object has no stored checksum; re-upload via the presigned PUT",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    partial = dest.parent / f"{dest.stem}.{uuid4().hex}.partial"
    effective = normalize_encoding(encoding)
    budget = _staging_budget(
        effective, object_size=head.size_bytes, uncompressed_size=uncompressed_size
    )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # After the mkdir (there is no filesystem to measure until the staging directory exists)
        # and before the partial is created, so a refused stage leaves nothing at all behind.
        _require_staging_free_space(dest, budget=budget, system_id=system_id)
        with _flocked_partial(partial) as guard_fd:
            _reserve_staging_space(
                guard_fd,
                partial=partial,
                dest=dest,
                budget=budget,
                system_id=system_id,
            )
            if effective is None:
                _stage_identity(
                    store,
                    key=object_key,
                    checksum=head.checksum_sha256,
                    partial_fd=guard_fd,
                    expected_bytes=head.size_bytes,
                    dest=dest,
                    system_id=system_id,
                )
            elif effective == GZIP_ENCODING:
                actual = _stage_gzip(
                    store,
                    key=object_key,
                    compressed_size=head.size_bytes,
                    checksum=head.checksum_sha256,
                    uncompressed_size=uncompressed_size,
                    partial_fd=guard_fd,
                    system_id=system_id,
                )
                os.ftruncate(guard_fd, actual)
            else:
                # Defence in depth: the declaration validator (ADR-0437) rejects an unknown codec,
                # so this is unreachable with valid data — but naming the codec beats silently
                # staging it as identity and failing with a misleading "not a qcow2" magic error.
                raise CategorizedError(
                    f"uploaded rootfs declared an unsupported transport encoding {effective!r}; "
                    "only gzip is supported",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"system_id": str(system_id)},
                )
            _require_still_linked(guard_fd, partial, window=_DOWNLOAD_WINDOW)
            _require_qcow2_magic(partial, system_id=str(system_id))
            if _sibling_already_published(dest):
                # Only reachable on the lost-session-lock path: the caller checked ``dest`` twice
                # under the fetch lock and found nothing, so a base appearing during the download
                # means a sibling held the lock and finished first. Its ``dest`` is the same
                # content-addressed, checksum-verified bytes as ours, and it may already back a
                # running guest's overlay — replacing it would orphan an inode of up to the 50 GiB
                # cap behind that guest's open descriptor for as long as it lives (ADR-0446 §5).
                _log.warning(
                    "a sibling published the staged rootfs base at %s while this fetcher was "
                    "downloading it; keeping the published base and discarding this copy "
                    "(system=%s) — the fetch lock was lost mid-transfer",
                    dest,
                    system_id,
                )
                return
            _durable_replace(partial, dest, system_id=system_id)
    except OSError as err:
        raise _staging_fault(dest, err, system_id=str(system_id)) from err
    finally:
        # Any failure that unwinds this frame discards the SENSITIVE partial here, in the
        # ``finally`` ADR-0441 §5 specifies (``os.replace`` consumed it on success, so this is then
        # a no-op). A *killed* worker unwinds nothing — SIGTERM sets an asyncio stop Event rather
        # than raising into this thread — so the sweeps remain the only collector for that case.
        _discard(partial)


def _stage_identity(
    store: UploadObjectStore,
    *,
    key: str,
    checksum: str,
    partial_fd: int,
    expected_bytes: int,
    dest: Path,
    system_id: UUID,
) -> int:
    """Stream an unencoded upload verbatim into the partial, verifying its SHA-256.

    The object is read in ``_STREAM_CHUNK_BYTES`` windows off a single unconditional GET (``etag``
    is ``None``, ADR-0054: the provision plane holds no client handle), each chunk hashed and
    written as it arrives, so peak memory is the chunk rather than the object — a multi-GiB rootfs
    no longer spikes the worker (#1520). A read may return fewer bytes than asked without being
    end-of-stream (the store's body wrapper is a ``RawIOBase``); only a true empty read ends the
    loop.

    The checksum is verified here, before the caller's shared qcow2-magic gate, which is the order
    ADR-0438 §3 and ADR-0441 §5 specify: a mismatch keeps ADR-0434 §2's ``INFRASTRUCTURE_FAILURE``
    rather than being reported by whichever gate a corrupt object happens to trip first. ADR-0445
    settled that category, which the gzip path used to answer differently for the byte-identical
    failure (#1523): it is retryable on both paths, because the recomputed hash covers transient
    GET-side transport corruption as well as permanent post-PUT bit rot, and the message says so
    in the same words ``strip_gzip_to_writer`` uses.
    """
    hasher = hashlib.sha256()
    written = 0
    writer_fd = os.dup(partial_fd)
    with store.get_artifact_stream(key, None) as fetched, os.fdopen(writer_fd, "wb") as writer:
        os.lseek(writer.fileno(), 0, os.SEEK_SET)
        while chunk := fetched.reader.read(_STREAM_CHUNK_BYTES):
            next_written = written + len(chunk)
            if next_written > expected_bytes:
                raise CategorizedError(
                    "uploaded rootfs object length changed between HEAD and GET; retry, and if "
                    "it persists repair the object-store boundary",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={
                        "system_id": str(system_id),
                        "dest": str(dest),
                        "expected_bytes": expected_bytes,
                        "actual_bytes": next_written,
                    },
                )
            hasher.update(chunk)
            writer.write(chunk)
            written = next_written
    if base64.b64encode(hasher.digest()).decode("ascii") != checksum:
        _log_checksum_mismatch(key, system_id=system_id, encoding="identity")
        raise CategorizedError(
            "uploaded rootfs object failed checksum verification: the stored bytes do not match "
            "the checksum signed at upload; retry, and if it persists the stored object is "
            "damaged and must be re-uploaded",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    if written != expected_bytes:
        raise CategorizedError(
            "uploaded rootfs object length changed between HEAD and GET; retry, and if it "
            "persists repair the object-store boundary",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "system_id": str(system_id),
                "dest": str(dest),
                "expected_bytes": expected_bytes,
                "actual_bytes": written,
            },
        )
    return written


def _log_checksum_mismatch(
    key: str, *, system_id: UUID, encoding: str, decode_detail: str | None = None
) -> None:
    """Log an object-store integrity failure, which the category alone no longer distinguishes.

    Same reasoning as :func:`_require_staging_free_space`'s warning, for the same reason: ADR-0445
    moved this condition out of the small ``CONFIGURATION_ERROR`` bucket and into
    ``INFRASTRUCTURE_FAILURE``, which in this tree is the catch-all for every store, libvirt, disk
    and capacity fault. Without this line an operator watching for stored-object damage — the
    permanent bit-rot mode ADR-0445 §1 names — cannot separate it from routine transient infra
    noise in ``record_job_failure`` without pulling each job's ``failure_context`` back through MCP.

    The retryable verdict makes that worse, not better: the agent is now *told* to retry, so the
    first observable consequence of real damage would otherwise be a silent extra multi-GiB
    download and a second dead-lettered job, with nothing in the host logs naming the condition.

    **Reach** (ADR-0523, closing ADR-0445 §6): this fires exactly where the checksum gate fires,
    and since that gate now runs ahead of the gzip path's framing and bound checks, the two paths
    log the same damage — under ADR-0445 §6 framing-first damage raised an object-defect error on
    the gzip path and logged nothing at all. What the widening does *not* buy is an inference from
    silence. Absence of this line means the digest agreed **or the verification never ran**: a
    reusable staged base short-circuits before any fetch, the free-space precheck and the
    missing-checksum branch raise before the first read, and a ``get_range`` fault — which
    :func:`_stage_gzip` calls the likeliest failure on this path — aborts the stage before a digest
    exists. Silence is evidence of an intact object only for a stage that read the object through.

    ``decode_detail`` is the gzip path's decode diagnosis, when the digest disagreed *and* the
    stream was also unreadable. Since ADR-0523 every shape of gzip stored-byte damage lands on this
    one line, so without it "truncated", "corrupt deflate", "trailing data" and "blew the declared
    bound" — materially different store failures — would be indistinguishable to an operator, on
    exactly the condition the same change makes far more common. It rides the exception chain
    rather than ``CategorizedError.details``, which is surfaceable to the agent: the remediation
    inside the carried text is the *decode's*, and is the wrong advice for a rotted object, which
    is why the "the decode also failed" framing scopes it and the agent never sees it.
    """
    _log.warning(
        "uploaded rootfs object %s failed checksum verification while staging for system %s "
        "(%s encoding): the stored bytes do not hash to the checksum signed at upload — transient "
        "read corruption clears on retry, a persistent mismatch means the stored object is "
        "damaged%s",
        key,
        system_id,
        encoding,
        f"; the decode also failed: {decode_detail}" if decode_detail else "",
    )


def _stage_gzip(
    store: UploadObjectStore,
    *,
    key: str,
    compressed_size: int,
    checksum: str,
    uncompressed_size: int | None,
    partial_fd: int,
    system_id: UUID,
) -> int:
    """Stream-gunzip a gzip transport object to the partial, bounded and transport-hash verified.

    ``strip_gzip_to_writer`` is consumer-agnostic and raises with empty ``details``, so its errors
    are annotated with the ``system_id`` every other raise in this module carries — otherwise a
    gzip staging failure lands in the job row without the one field an operator pivots on to
    correlate it to a System, while the byte-identical identity failure lands with it (ADR-0445 §4).
    """
    if uncompressed_size is None:
        raise CategorizedError(
            "uploaded rootfs declared a gzip encoding without an uncompressed_size; re-declare the "
            "upload with the canonical object size",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id)},
        )
    request = StripDecodeRequest(
        key=key,
        compressed_size=compressed_size,
        expected_sha256=checksum,
        uncompressed_size=uncompressed_size,
    )
    try:
        writer_fd = os.dup(partial_fd)
        with os.fdopen(writer_fd, "wb") as writer:
            os.lseek(writer.fileno(), 0, os.SEEK_SET)
            result = strip_gzip_to_writer(store, request, writer)
    except CategorizedError as exc:
        exc.details.setdefault("system_id", str(system_id))
        # Keyed on the gate marker, NOT on the category. ``strip_gzip_to_writer`` calls
        # ``get_range`` uncaught, so the store's own ``INFRASTRUCTURE_FAILURE`` — a connection reset
        # on any of the hundreds of ranged GETs a multi-GiB stage issues, by far the likeliest
        # failure on this path — propagates through it. A category test would log every such blip as
        # stored-object damage, which is exactly the discrimination this log exists to provide.
        if exc.details.get("gate") == TRANSPORT_CHECKSUM_GATE:
            # ``__cause__`` is the decode's own diagnosis when the stream was unreadable *and* the
            # digest disagreed — the detail ADR-0523 keeps out of the agent's message and out of
            # ``details``, and the only thing distinguishing the shapes of damage that now all land
            # on this one line. ``None`` when the object decoded cleanly and only the hash differed.
            _log_checksum_mismatch(
                key,
                system_id=system_id,
                encoding=GZIP_ENCODING,
                decode_detail=None if exc.__cause__ is None else str(exc.__cause__),
            )
        raise
    return result.uncompressed_bytes


def _starts_with_qcow2_magic(staged: Path) -> bool:
    """Whether ``staged``'s first bytes are the qcow2 magic; a file too short to hold it is not.

    The single implementation of the format probe, shared by the staging gate below and the reuse
    gate in :func:`_staged_base_rejection` so the two can never drift apart. ``OSError`` propagates
    — each caller decides what an unreadable file means for it.
    """
    with staged.open("rb") as reader:
        return reader.read(len(_QCOW2_MAGIC)) == _QCOW2_MAGIC


#: Why a present staged base may not be reused, as a slug the call sites render into their WARNING.
#: A bare bool was enough while the gate had one reason; ADR-0451 gave it three, and they mean
#: *opposite* things to an operator — a failed format gate says the durability bug fired or the base
#: was corrupted by some other means, while a missing marker on an otherwise perfect base is the
#: expected one-time cost of upgrading and needs no action. Reporting the second as the first is the
#: same defect the two commits before this branch fixed one gate over (the checksum mismatch
#: keyed on the category rather than on the gate), so it is keyed on the gate here too.
_NOT_A_REGULAR_FILE = "not_a_regular_file"
_NO_COMPLETION_MARKER = "no_completion_marker"
_FAILED_FORMAT_GATE = "failed_format_gate"
_REJECTION_PROSE = {
    _NOT_A_REGULAR_FILE: "is not a regular file",
    _NO_COMPLETION_MARKER: (
        "has no completion marker, so nothing attests that its stage ever finished durably — "
        "expected once per base when upgrading past ADR-0451, and otherwise a sibling publishing "
        "this base right now (between its rename and its marker write), a crash mid-stage, or a "
        "publish whose marker write itself faulted"
    ),
    _FAILED_FORMAT_GATE: "did not re-pass the qcow2 format gate",
}
