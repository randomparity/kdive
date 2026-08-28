"""Publish and validate crash-durable staged rootfs bases."""

from __future__ import annotations

import logging
import os
import stat
from contextlib import suppress
from pathlib import Path

# The function rather than the module, so a test's monkeypatch is scoped to this module instead of
# replacing ``shutil.disk_usage`` process-wide for every other importer for the test's duration.
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.runtime_paths import staged_rootfs_marker_path

_log = logging.getLogger(__name__)

_TENANT = "local"
_OWNER_KIND = "investigations"
# The qcow2 magic every canonical rootfs base must start with (bytes ``51 46 49 fb``); a base that
# does not is rejected here rather than failing late and confusingly at ``qemu-img`` (ADR-0438).
_QCOW2_MAGIC = b"QFI\xfb"
# The identity path's per-read window: the staging loop hashes and writes one chunk at a time, so

_NOT_A_REGULAR_FILE = "not_a_regular_file"
_NO_COMPLETION_MARKER = "no_completion_marker"
_FAILED_FORMAT_GATE = "failed_format_gate"
_REJECTION_PROSE = {
    _NOT_A_REGULAR_FILE: "is not a regular file",
    _NO_COMPLETION_MARKER: "has no completion marker proving a durable stage",
    _FAILED_FORMAT_GATE: "did not re-pass the qcow2 format gate",
}


def _starts_with_qcow2_magic(staged: Path) -> bool:
    with staged.open("rb") as reader:
        return reader.read(len(_QCOW2_MAGIC)) == _QCOW2_MAGIC


def _staged_base_rejection(dest: Path, *, system_id: UUID) -> str | None:
    """Why a present staged base may not back an overlay, or ``None`` when it may (#1526, #1539).

    The reuse fast path used to treat *any* present ``dest`` as authoritative (a bare
    ``dest.is_file()``), which bypassed every verification gate exactly when the file was most
    suspect: a base torn by a crash mid-stage is full-length and unverified, and under ADR-0441 §5
    content-addressed reuse it would then silently back every System in the investigation until
    close — with the checksum machinery skipped *because* the file existed.

    The re-check is the qcow2-magic probe the staging path already applies. It is O(1), so it stays
    affordable on the per-System provision hot path, and it catches the truncated, empty, garbage,
    and whole-file-zeroed shapes. It is deliberately **not** a checksum re-verify — that is
    O(filesize) against a base of tens of GiB, on every guest start, which would undo the point of
    staging once per investigation. It is also deliberately not a size comparison:
    ``artifacts.uncompressed_size`` is an upper *bound* rather than an exact size
    (``strip_gzip_to_writer`` caps output at it and accepts less) and is NULL on the identity path,
    so an equality gate would false-reject a good base and re-download it on every provision.

    **The magic probe alone is not a crash-torn-base detector** (ADR-0443 §3), which is why
    ADR-0451 added the completion marker above it. Damage past the first four bytes passes the
    probe, and the rename follows the *completed* write — so writeback has already flushed most of a
    multi-GiB base by then and the dirty residue at crash time is its **tail**. The expected large
    crash survivor is head-intact and tail-zeroed, and it passes the probe. The marker is what
    rejects it: a crash before :func:`_durable_replace` reaches its marker write leaves no marker
    regardless of what the base looks like.

    **The magic probe is nonetheless kept, not replaced.** The marker is a *completion* witness, not
    an *integrity* one — it says a stage ran to a durable finish, and says nothing about damage
    arriving after the publish. A dying disk, a stray ``cp``, a half-restored backup: ADR-0443 §3's
    second population, for which the probe is still the only net on this path. It costs a 4-byte
    read on a path that opens the base for ``qemu-img`` moments later regardless.

    Not reusable, without reading anything: the path is absent, a non-directory sits on its parent
    path, a directory sits on its own, what is there is **not a regular file**, or the marker is
    missing. The regular-file test is why the mode is checked before the ``open`` rather than left
    to the error taxonomy — opening a FIFO for reading blocks until a writer appears, so a probe
    that skipped the ``S_ISREG`` test would hang the provision thread forever, and the post-lock
    call site would hang *holding* the fetch advisory lock, wedging every sibling System on that
    (investigation, checksum). Nothing in kdive creates a non-regular file here, but
    ``dest.is_file()`` rejected one for free and this must not regress into a hang. The marker needs
    no such argument because it is only ever ``stat``\\ ed, never opened.

    Every other ``OSError`` — from either ``stat`` or the ``open`` — is raised as an
    ``INFRASTRUCTURE_FAILURE``: a base that is present but unreadable (``EACCES`` under a
    worker/staging-user asymmetry of the shape ADR-0442 documents, ``EMFILE`` under descriptor
    exhaustion, a transient ``EIO``) is an operator-visible fault, **not** a cache miss. This is the
    one place the gate is deliberately *narrower* than the ``dest.is_file()`` it replaces, which
    swallowed every ``OSError`` alike: treating those as a cache miss would swap a good multi-GiB
    base out from under any guest holding it (see the residue in ADR-0443 §2) and re-download it on
    every provision, silently, for as long as the fault lasts.

    Returns:
        ``None`` when the base may be reused, or one of :data:`_REJECTION_PROSE`'s slugs naming the
        gate that rejected it. Which gate is not diagnostic colour: on the first provision after an
        upgrade **every** base in the tree is rejected for a missing marker, and reporting that as a
        format-gate failure would tell an operator the durability bug had fired on a base that is
        perfectly intact.
    """
    try:
        if not stat.S_ISREG(dest.stat().st_mode):
            return _NOT_A_REGULAR_FILE
        if not _completion_marker_present(dest, system_id=system_id):
            return _NO_COMPLETION_MARKER
        if not _starts_with_qcow2_magic(dest):
            return _FAILED_FORMAT_GATE
    except FileNotFoundError, NotADirectoryError, IsADirectoryError:
        # An absent base, or an absent parent. The call sites gate their WARNING on
        # ``dest.exists()``, so this slug is never rendered for it — the ordinary cache miss stays
        # silent, which is what keeps the log line meaning "something was there and was rejected".
        return _NOT_A_REGULAR_FILE
    except OSError as err:
        raise _unreadable_base_fault(dest, err, system_id=str(system_id), probed=dest) from err
    return None


def _completion_marker_present(dest: Path, *, system_id: UUID) -> bool:
    """Whether ``dest``'s completion marker attests that a stage of it finished durably (ADR-0451).

    Handles its own errors rather than deferring to the caller's ladder so the raised fault names
    the path that actually failed: an operator sent to inspect the multi-GiB base when it was the
    zero-byte marker beside it that could not be ``stat``\\ ed has been given the wrong file.

    The taxonomy is otherwise :func:`_staged_base_rejection`'s, for its reasons. An absent marker
    (or an absent parent) means *there is no completed stage at this path* and is a cache miss; a
    marker that cannot be ``stat``\\ ed at all is the ADR-0442 uid asymmetry or a transient ``EIO``,
    and answering that as a cache miss would produce the silent, perpetual, fetch-lock-serialized
    re-download loop ADR-0443 decision 2 exists to refuse. A *directory* at the marker path is
    rejected by ``S_ISREG`` rather than raising; nothing in kdive creates one.
    """
    marker = staged_rootfs_marker_path(dest)
    try:
        return stat.S_ISREG(marker.stat().st_mode)
    except FileNotFoundError, NotADirectoryError:
        return False
    except OSError as err:
        raise _unreadable_base_fault(dest, err, system_id=str(system_id), probed=marker) from err


def _sibling_already_published(dest: Path) -> bool:
    """Reuse only a regular, marked, qcow2 base; an unreadable base must be republished."""
    try:
        return (
            stat.S_ISREG(dest.stat().st_mode)
            and stat.S_ISREG(staged_rootfs_marker_path(dest).stat().st_mode)
            and _starts_with_qcow2_magic(dest)
        )
    except OSError:
        return False


def _unreadable_base_fault(
    dest: Path, err: OSError, *, system_id: str, probed: Path
) -> CategorizedError:
    """The ``INFRASTRUCTURE_FAILURE`` for a staged base that is present but cannot be read.

    Deliberately *not* :func:`_staging_fault`. Nothing is being staged on this path — no lock taken,
    no object HEADed, no stream opened — so "failed to stage the uploaded rootfs" would point an
    operator at the download and the object store. The likeliest trigger is the worker/staging-user
    permission asymmetry ADR-0442 documents in this same subsystem, where the actionable fix is the
    ownership of a file that is already present and probably intact.

    ``probed`` is the file the fault actually came from — the base, or the completion marker beside
    it (ADR-0451 §3). The two have different remedies and are owned by the same code, so naming the
    base while the marker is what failed sends an operator to inspect the wrong one; it is carried
    in ``details`` as well as in the message, because ``_failure_context`` copies those scalars into
    the job row an operator triages from.
    """
    return CategorizedError(
        f"the staged uploaded rootfs base at {str(dest)!r} is present but could not be read "
        f"({err.strerror} on {str(probed)!r}); it is not treated as a cache miss, because silently "
        "re-downloading it would supersede a base that may still be backing running guests",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"system_id": system_id, "dest": str(dest), "probed": str(probed)},
    )


def _fsync_path(path: Path, flags: int) -> None:
    """``fsync`` whatever ``path`` names, opening it just for the sync and always closing it.

    The stager's own writer is already closed by the time this runs, so its bytes are in the page
    cache and a fresh descriptor on the same inode flushes exactly the same data. ``flags`` is
    ``O_WRONLY`` for the partial and ``O_RDONLY`` for a directory: POSIX leaves ``fsync`` on a
    read-only descriptor free to return ``EBADF``, so a file is never synced through one.
    """
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(partial: Path, dest: Path, *, system_id: UUID) -> None:
    """Durably publish a verified partial, then its completion marker (ADR-0443/0451).

    The fsync order guarantees either no marker (re-stage) or a marker over durable base data.
    """
    marker = staged_rootfs_marker_path(dest)
    _fsync_path(partial, os.O_WRONLY)
    _clear_completion_marker(marker, dest, system_id=system_id)
    _fsync_path(dest.parent, os.O_RDONLY)
    os.replace(partial, dest)
    _fsync_path(dest.parent, os.O_RDONLY)
    _write_completion_marker(marker, dest, system_id=system_id)
    _fsync_path(dest.parent, os.O_RDONLY)


def _clear_completion_marker(marker: Path, dest: Path, *, system_id: UUID) -> None:
    """Remove any stale completion marker before the rename publishes a new base (ADR-0451 §2)."""
    try:
        marker.unlink(missing_ok=True)
    except OSError as err:
        raise _marker_fault(
            marker,
            dest,
            err,
            system_id=str(system_id),
            consequence=(
                f"nothing was published, so the base at {str(dest)!r} is unchanged; until this "
                "marker can be removed no stage of this base can be published, because a marker "
                "left over the rename would attest to a base this stage had already rejected"
            ),
        ) from err


def _write_completion_marker(marker: Path, dest: Path, *, system_id: UUID) -> None:
    """Create the zero-byte completion marker and ``fsync`` it (ADR-0451 §1/§2).

    ``O_TRUNC`` rather than ``O_EXCL`` because :func:`_durable_replace` has just unlinked any stale
    marker and a race there would be a second fetcher publishing the same content-addressed base;
    failing the provision over it would be a worse answer than converging on the same empty file.

    The mode is ``0o666`` — umask applied — matching the partial that becomes the base, so the
    marker cannot acquire a permission asymmetry with the file it attests to under the mixed-uid
    deployments ADR-0442 documents. The ``fsync`` flushes only an inode, since the file has no data
    blocks; it costs one syscall pair and removes the need to reason about whether a zero-length
    file's inode is covered by the directory sync that follows.

    This is the one step of the publish that **creates a directory entry**, so it carries failure
    modes none of the six around it can produce: ``ENOSPC``/``EDQUOT`` from inode or directory-block
    exhaustion — ADR-0450's precheck reserves *blocks*, so its floor does not prevent it —
    ``EROFS`` after a remount-ro on a disk that is already faulting, ``EMFILE``/``ENFILE`` under
    the descriptor exhaustion :func:`_unreadable_base_fault` already names as realistic, and
    ``EISDIR``
    if anything ever occupies the marker path. It must stay **fatal**: succeeding with a marker-less
    base would hide the re-download loop below rather than report it.

    **All three syscalls are inside the fault region, not just the ``open``.** ``fsync`` is where
    Linux surfaces a *deferred* writeback error (``errseq_t``), so ``EIO`` here is the normal
    reporting point on exactly the failing-disk host this docstring already names — and ``close``
    reports it too on some filesystems. Leaving either outside would send them through
    :func:`_staging_fault` and name the published base, which is the misattribution this whole pair
    of helpers exists to remove. The two arms leave different states and say so.
    """
    try:
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o666)
    except OSError as err:
        raise _marker_fault(
            marker,
            dest,
            err,
            system_id=str(system_id),
            consequence=(
                f"the base at {str(dest)!r} is published, verified and durable — only this "
                "marker is missing, so the next fetch rejects that base and re-downloads the "
                "whole object; a persistent fault here re-downloads it on every attempt"
            ),
        ) from err
    try:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as err:
        raise _marker_fault(
            marker,
            dest,
            err,
            system_id=str(system_id),
            consequence=(
                f"the base at {str(dest)!r} is published, verified and durable and this marker "
                "exists, but its durability is unproven — a crash before the next writeback loses "
                "it and the next fetch re-stages the base, which is the safe direction"
            ),
        ) from err


def _marker_fault(
    marker: Path, dest: Path, err: OSError, *, system_id: str, consequence: str
) -> CategorizedError:
    """The ``INFRASTRUCTURE_FAILURE`` for a completion marker that could not be written or removed.

    Deliberately *not* :func:`_staging_fault`, for the reason :func:`_unreadable_base_fault` gives
    on the read side of this same pair: "failed to stage the uploaded rootfs to ``<token>.qcow2``"
    names a multi-GiB base that is present, complete and — past the rename — durable, when the
    actionable
    file is the zero-byte marker beside it. Both the message and ``details`` carry the marker, and
    the ``consequence`` says what state the caller is left in, because the two sites leave opposite
    ones: before the rename nothing was published, and after it a good base was.
    """
    return CategorizedError(
        f"failed to update the staged rootfs completion marker {str(marker)!r} "
        f"({err.strerror}); {consequence}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"system_id": system_id, "dest": str(dest), "marker": str(marker)},
    )


def _require_qcow2_magic(staged: Path, *, system_id: str) -> None:
    """Reject a staged base that does not start with the qcow2 magic (ADR-0438).

    Reads the finished ``.partial``, so both codecs are gated by one mechanism over the canonical
    bytes — each stager has already verified its own checksum, and a base too short to hold the
    magic yields a short read that fails here rather than staging unchecked.
    """
    if not _starts_with_qcow2_magic(staged):
        raise CategorizedError(
            "staged rootfs is not a qcow2 image: the uploaded object (after any transport decode) "
            "does not start with the qcow2 magic; upload a qcow2 image",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": system_id},
        )


def _discard(tmp: Path) -> None:
    """Best-effort removal of a partial staging file so a raised error leaves no orphan."""
    with suppress(OSError):
        tmp.unlink()


def _staging_fault(dest: Path, err: OSError, *, system_id: str) -> CategorizedError:
    """The uniform ``INFRASTRUCTURE_FAILURE`` for an IO fault while staging the rootfs base."""
    return CategorizedError(
        f"failed to stage the uploaded rootfs to {str(dest)!r}: {err.strerror}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"system_id": system_id, "dest": str(dest)},
    )
