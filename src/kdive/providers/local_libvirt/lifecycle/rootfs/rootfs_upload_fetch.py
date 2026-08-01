"""Investigation-scoped uploaded-rootfs provision-time fetch (ADR-0441, ADR-0434, ADR-0438).

Wires the ``upload`` rootfs lane: an investigation-owned uploaded qcow2 (ADR-0441 §1) is resolved
by content address within the provisioning System's own investigation, downloaded from the object
store, and staged to a checksum- and format-verified local path shared by every System in the
investigation. Mirrors ``rootfs_catalog_fetch_from_env`` — a synchronous callable that lazily opens
its resources per call, because the provider provision seam runs off the event loop
(``asyncio.to_thread``) and owns no async pool.

Resolution (ADR-0441 §4): the profile's canonical-base64 ``checksum_sha256`` is transcoded to the
base64url object token; the object key
``artifact_key("local","investigations",<inv>,"rootfs-<token>")`` is looked up pinned to the
System's own ``investigation_id`` (the isolation boundary). The declared transport ``encoding`` is
read from that durable ``artifacts`` row (finalize deletes the manifest), and a ``gzip`` upload is
streamed-decompressed to the staged base; an identity upload stages verbatim. Either way the
canonical base is qcow2-magic-validated before it backs an overlay.

Durability (ADR-0443, ADR-0451): the partial is ``fsync``\\ ed before the ``os.replace`` publishes
it and the staging directory ``fsync``\\ ed after, so a host crash cannot leave a durable rename
over non-durable data; and the reuse fast path re-verifies rather than trusting a present file, so a
base torn by a crash that predates this cannot back an overlay. That re-verification is a zero-byte
``<token>.ready`` completion marker written and synced *after* the base is durable, **plus** the
qcow2-magic gate. The marker is what catches the expected crash-torn shape — head-intact and
tail-zeroed, which the magic gate passes (ADR-0443 §3) — because a crash before the marker write
leaves no marker whatever the base looks like. The magic gate is kept beside it because the marker
witnesses *completion*, not *integrity*, and says nothing about damage arriving after the publish. A
rejection is logged — the re-stage succeeds, so the log line is the only evidence it ever fired —
while a base that is present but *unreadable* is an ``INFRASTRUCTURE_FAILURE``, never a cache miss.

Capacity (ADR-0450/0530): a stage the staging filesystem plainly cannot hold is refused before its
first byte (:func:`_require_staging_free_space`), then the guarded partial reserves the base's
budget with native ``fallocate(2)``. Different-base siblings remain parallel, while the filesystem
admits only reservations whose blocks fit. A filesystem without native allocation support degrades
loudly to ADR-0450's advisory precheck; KDIVE never invokes an emulated ``posix_fallocate``. The
one-GiB floor remains advisory against unrelated writers, and mid-write faults still flow through
:func:`_staging_fault`.

Concurrency (ADR-0441 §5): the shared per-(investigation, checksum) staging path means two sibling
Systems can provision at once. Each fetcher writes a **unique** ``<token>.<uuid>.partial`` and
``os.replace``s it onto ``<token>.qcow2`` only after verify, so no two downloaders share a
partial — the correctness guarantee. A **session-scoped** ``pg_advisory_lock`` (keyed via
``db.locks._session_lock_key``, held on this call's dedicated sync connection across the download)
collapses the redundant multi-GiB download, and a crash-orphaned ``<token>.*.partial`` is
glob-unlinked opportunistically on the next fetch of that base.

That sweep is gated on an ``flock``, not on the fetch lock (ADR-0446): a session lock belongs to a
Postgres *connection*, which is idle for the whole download and can be reaped while its owner keeps
writing, so a sibling could acquire the lock and unlink a **live** partial. Each fetcher instead
holds an exclusive ``flock`` on its own partial (:func:`_flocked_partial`) and the sweep skips what
it cannot lock — while the kernel's release-on-exit keeps a killed worker's orphan collectable with
no timeout to wait out.
"""

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
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

# The function rather than the module, so a test's monkeypatch is scoped to this module instead of
# replacing ``shutil.disk_usage`` process-wide for every other importer for the test's duration.
from shutil import disk_usage
from typing import Protocol
from uuid import UUID, uuid4

import psycopg

import kdive.config as config
from kdive.artifacts import storage as artifact_types
from kdive.artifacts.content_address import rootfs_object_name, rootfs_object_token
from kdive.artifacts.transport_encoding import (
    GZIP_ENCODING,
    TRANSPORT_CHECKSUM_GATE,
    StripDecodeRequest,
    normalize_encoding,
    strip_gzip_to_writer,
)
from kdive.config.core_settings import DATABASE_URL
from kdive.db.locks import _session_lock_key
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    UploadFetch,
    staged_rootfs_path,
)
from kdive.providers.shared.rootfs_fetch_leases import acquire_fetch_lease, release_fetch_lease
from kdive.providers.shared.runtime_paths import staged_rootfs_marker_path
from kdive.providers.shared.staging_partials import unlink_partial_if_unheld
from kdive.store.objectstore import artifact_key, object_store_from_env

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


class UploadObjectStore(Protocol):
    """The narrow object-store capability the upload fetch needs (an :class:`ObjectStore`).

    Neither staging path buffers the whole object: ``get_artifact_stream`` (ADR-0400) backs the
    identity path's chunked read, and ``get_range`` satisfies
    :class:`transport_encoding.RangedReadStore` so a gzip upload can be streamed-decompressed. The
    whole-object ``get_artifact`` is deliberately absent — a multi-GiB rootfs must never be
    materialized as ``bytes`` in a worker.
    """

    def head(self, key: str) -> artifact_types.HeadResult | None: ...
    def get_artifact_stream(
        self, key: str, etag: str | None
    ) -> AbstractContextManager[artifact_types.StreamedArtifact]: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ResolvedRootfsObject:
    """The committed investigation-rootfs object a System resolves (ADR-0441 §4)."""

    object_key: str
    encoding: str | None
    uncompressed_size: int | None


def _fetch_lock_name(investigation_id: UUID, token: str) -> str:
    """The deterministic per-(investigation, checksum) fetch-serialization lock name (ADR-0441 §5).

    Keyed via :func:`kdive.db.locks._session_lock_key` (the session keyspace, salted apart from the
    transaction-scope keyspace) — **not** Python ``hash()``, which is per-process salted and would
    derive a different key in each worker process, silently no-op the lock, and re-admit the double
    download.
    """
    return f"rootfs-fetch:{investigation_id}:{token}"


def rootfs_upload_fetch_from_env() -> UploadFetch:
    """A synchronous ``(RootfsUploadContext) -> Path`` uploaded-rootfs fetch (ADR-0441).

    Opens a short-lived **autocommit** sync ``psycopg`` connection per call to resolve the System's
    investigation and the committed object (the provision seam runs in a thread and owns no async
    pool; the catalog fetch, ADR-0228, opens its own sync connection the same way). Autocommit so
    the session advisory lock held across the multi-GiB download never keeps a transaction open (an
    ``advisory_xact_lock`` would trip ``idle_in_transaction_session_timeout``). A present staged
    file is reused once it re-passes the format gate (ADR-0443). S3 is a required backend
    (ADR-0337).
    """

    def _fetch(upload: RootfsUploadContext) -> Path:
        store = object_store_from_env()
        with psycopg.connect(config.require(DATABASE_URL), autocommit=True) as conn:
            return fetch_uploaded_rootfs(conn, store, upload)

    return _fetch


def fetch_uploaded_rootfs(
    conn: psycopg.Connection,
    store: UploadObjectStore,
    upload: RootfsUploadContext,
) -> Path:
    """Resolve + stage the investigation-scoped uploaded rootfs to a verified local path.

    Resolves the object by content address within the System's own investigation, reuses a present
    staged file that :func:`_staged_base_rejection` accepts, and otherwise downloads it once under
    a session advisory lock. The re-check runs on both sides of the lock, so neither the fetcher
    that finds a torn base nor the one that queues behind a sibling can be handed one (ADR-0443).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the System has no investigation binding, the
            checksum is not owned by the investigation, the object was never uploaded, or the
            canonical base is not a qcow2; ``INFRASTRUCTURE_FAILURE`` on a missing/mismatched
            checksum, a staging IO fault, or a staged base that is present but unreadable
            (:func:`_unreadable_base_fault` — deliberately *not* a staging fault, because nothing
            was being staged and the remedy is the file, not the object store).
    """
    token = rootfs_object_token(upload.checksum_sha256)
    investigation_id = _resolve_investigation(conn, upload.system_id)
    # Before _resolve_object, not after, and that ordering is the whole point (ADR-0515, #1702).
    # The instant this fetch resolves its artifacts row it is a download a reclaim must not delete
    # under, and it then waits on the session advisory lock below — behind a sibling's entire
    # multi-GiB transfer — before it creates the partial ADR-0495's probe looks for. Taking the
    # lease one line later would leave exactly that window open while looking correct.
    lease_id = acquire_fetch_lease(
        conn, investigation_id, token, system_id=upload.system_id, job_id=upload.job_id
    )
    try:
        return _fetch_under_lease(
            conn, store, upload, investigation_id=investigation_id, token=token
        )
    finally:
        if lease_id is not None:
            release_fetch_lease(conn, lease_id)


def _fetch_under_lease(
    conn: psycopg.Connection,
    store: UploadObjectStore,
    upload: RootfsUploadContext,
    *,
    investigation_id: UUID,
    token: str,
) -> Path:
    """Resolve the object and stage it, with this fetch's lease already recorded.

    Split from :func:`fetch_uploaded_rootfs` so the lease brackets **every** exit of this body —
    including the reuse fast path, whose early ``return`` would otherwise need its own release —
    without nesting the whole function in a ``try``.
    """
    resolved = _resolve_object(conn, investigation_id, token, upload)
    dest = staged_rootfs_path(investigation_id, token, upload_dir=upload.upload_dir)
    rejection = _staged_base_rejection(dest, system_id=upload.system_id)
    if rejection is None:
        return dest
    # The signal this change exists to produce. A rejected base is otherwise invisible: the re-stage
    # below succeeds, so no error is ever raised and the only symptom is a provision that took a
    # multi-GiB download longer than it should have. The line names **which** gate rejected it,
    # because the two mean opposite things to an operator — a failed format gate is the durability
    # bug firing or a base corrupted by some other means, while a missing marker is the expected
    # one-time cost of upgrading past ADR-0451 and needs no action at all.
    stale_on_arrival = dest.exists()
    if stale_on_arrival:
        _log.warning(
            "staged rootfs base at %s %s; re-staging it (investigation=%s system=%s)",
            dest,
            _REJECTION_PROSE[rejection],
            investigation_id,
            upload.system_id,
        )
    lock_key = _session_lock_key(_fetch_lock_name(investigation_id, token))
    conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
    try:
        rejection = _staged_base_rejection(dest, system_id=upload.system_id)
        if rejection is None:
            return dest  # a sibling fetcher finished while we waited on the lock
        if dest.exists() and not stale_on_arrival:
            # Gated on ``stale_on_arrival`` because nothing between the two checks repairs ``dest``:
            # without it, the ordinary stale-base rejection would emit this line too and assert a
            # racing sibling that never existed. Reaching it means ``dest`` really did appear *while
            # we waited* — a sibling just published a base that does not verify, which is a
            # materially louder condition than finding a stale one on arrival.
            _log.warning(
                "a sibling published a staged rootfs base at %s that %s; re-staging it "
                "(investigation=%s system=%s)",
                dest,
                _REJECTION_PROSE[rejection],
                investigation_id,
                upload.system_id,
            )
        _unlink_orphan_partials(dest)
        stage_uploaded_rootfs(
            store,
            object_key=resolved.object_key,
            dest=dest,
            encoding=resolved.encoding,
            uncompressed_size=resolved.uncompressed_size,
            system_id=upload.system_id,
        )
    finally:
        _release_fetch_lock(conn, lock_key)
    return dest


def _release_fetch_lock(conn: psycopg.Connection, lock_key: int) -> None:
    """Release the fetch lock, tolerating the session loss this whole guard is written about.

    Both triggers ADR-0446 names — a terminated backend, and a middlebox-evicted flow that later
    draws an ``RST`` — destroy the *client* connection at the instant they release the lock, so the
    unlock this issues is both redundant and doomed. Left bare it raised out of a ``finally`` and
    failed the provision one line after the flock guard had saved it, which is exactly the
    "degrades to a redundant download, never a failed provision" claim not holding.

    A ``finally`` also *replaces* any in-flight exception, so the bare call demoted an actionable
    ``CategorizedError`` — a checksum mismatch, a non-qcow2 upload — to ``__context__`` behind a
    Postgres admin-shutdown message, on precisely the runs where the connection had since died.

    Suppressing every ``psycopg.Error`` is the correct semantics rather than a convenience — raising
    out of a ``finally`` is the defect above. The *narration* is split, because the ``except`` is
    wider than the two triggers: a role or database ``statement_timeout`` cancels this statement as
    ``QueryCanceled`` on a session that is still open and **still holding the lock**. Reporting the
    observed connection state rather than the inferred cause keeps this from writing down a
    conditional as an invariant, which is the defect class this whole change exists to remove.
    """
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
    except psycopg.Error as err:
        if conn.closed:
            _log.warning(
                "could not release the rootfs fetch advisory lock (%s); its Postgres session is "
                "gone, which already released it — this fetch held the lock for less than it "
                "appeared to",
                err,
            )
        else:
            _log.warning(
                "could not release the rootfs fetch advisory lock (%s), and its Postgres session "
                "is still open — the lock may still be held, blocking every sibling fetch of this "
                "base until that session closes",
                err,
            )


def _resolve_investigation(conn: psycopg.Connection, system_id: UUID) -> UUID:
    """Resolve the provisioning System's investigation binding (ADR-0441 §2)."""
    with conn.cursor() as cur:
        cur.execute("SELECT investigation_id FROM systems WHERE id = %s", (system_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        raise CategorizedError(
            "upload-kind rootfs requires a System bound to an investigation; this System has no "
            "investigation_id",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id)},
        )
    return row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))


def _resolve_object(
    conn: psycopg.Connection,
    investigation_id: UUID,
    token: str,
    upload: RootfsUploadContext,
) -> ResolvedRootfsObject:
    """Resolve the committed object by content-addressed key within the investigation (ADR-0441 §4).

    The ``owner_id`` predicate is the isolation boundary and the derived-key match is the content
    address; a miss (the checksum is not owned by this investigation) fails fast with an actionable
    ``configuration_error`` naming the unresolved checksum.
    """
    object_key = artifact_key(
        _TENANT, _OWNER_KIND, str(investigation_id), rootfs_object_name(token)
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT encoding, uncompressed_size FROM artifacts "
            "WHERE owner_kind = %s AND owner_id = %s AND object_key = %s",
            (_OWNER_KIND, investigation_id, object_key),
        )
        row = cur.fetchone()
    if row is None:
        raise CategorizedError(
            "uploaded rootfs checksum is not owned by this System's investigation; finalize the "
            "upload (investigations.complete_rootfs_upload) in the investigation this System is "
            "bound to",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "system_id": str(upload.system_id),
                "checksum_sha256": upload.checksum_sha256,
                "investigation_id": str(investigation_id),
            },
        )
    return ResolvedRootfsObject(
        object_key=object_key,
        encoding=normalize_encoding(row[0]) if isinstance(row[0], str) else None,
        uncompressed_size=row[1],
    )


def _unlink_orphan_partials(dest: Path) -> None:
    """Glob-unlink each **unheld** crash-orphaned ``<token>.*.partial`` (ADR-0446).

    A killed worker's ``.partial`` is a SENSITIVE multi-GiB leak no row owns, so ADR-0441 §5 has a
    live fetcher collect it opportunistically — bounding it by the *next fetch* of that base rather
    than by full investigation reclaim.

    What it must not collect is a partial some sibling is still writing. ADR-0441 §5 justified the
    unconditional unlink on holding the fetch lock, "which serializes downloads so no *live* sibling
    exists" — but that lock is a **session** ``pg_advisory_lock``, a property of a Postgres
    connection rather than of the process that took it, and the connection sends nothing for the
    whole multi-GiB download. An idle-connection reap or a terminated backend releases it while the
    owner is still writing; a sibling then acquires it, swept here, and the first fetcher wrote on
    into an unlinked inode and failed at ``os.replace`` — a failed provision, with the written
    blocks charged to ``df`` yet invisible to every path-matching tool.

    So liveness is asked of the kernel instead: a live writer holds an exclusive ``flock`` on its
    own partial (:func:`_flocked_partial`), and a candidate this cannot lock is skipped. The gate
    costs nothing in reach, because the kernel drops an ``flock`` when the holding descriptor is
    closed — including on process exit, normal or ``SIGKILL`` — so a crash orphan is already
    unlocked by the time any sibling sweeps it and needs no timeout to age out. Correctness no
    longer depends on the fetch lock at all; do not re-derive it from the lock, and do not widen the
    glob over the whole uploads dir.

    The ``suppress`` covers the directory walk only — every per-candidate fault is handled inside
    :func:`~kdive.providers.shared.staging_partials.unlink_partial_if_unheld`, so a single
    unsweepable file cannot truncate the pass. Its "held" answer is discarded here: this sweep is
    opportunistic and bounded by the next fetch, so a skip needs no follow-up. The reclaim-side
    backstop consumes it, because there the skip decides whether the investigation's drain marker
    clears (ADR-0452 §4).
    """
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
    """Raise if the partial behind ``fd`` has been unlinked, so the fault names the sweep.

    Called twice, with a different ``window`` each time because the two mean different things to
    an operator — nothing can take a partial "between its creation and its lock" minutes after
    that lock succeeded. At creation it closes the two-syscall window ADR-0446 §3 describes.
    After the download the cause is one, and it is not that window: the :func:`_flocked_partial`
    degrade path, where the filesystem could not lock and so nothing kept a sweeper out for the
    whole transfer — §5's symmetry argument (a sweeper there cannot lock either) is evaluated at one
    instant and not maintained across minutes, and a recovering ``lockd`` falsifies it mid-download.
    Either sweep can then take the partial: the reclaim-side backstop was a *second, independent*
    cause until ADR-0452 (#1544) gave it the same ``flock`` gate, and it is now reachable only
    through this same degrade.

    The degrade path's outcome is then the pre-ADR-0446 one, which is the point of degrading rather
    than failing. What this adds is the diagnosis: without it the fetcher streams on and dies at
    ``os.replace`` with a bare ``ENOENT`` that :func:`_staging_fault` renders as "failed to stage",
    pointing an operator at the object store over a purely local race — exactly the misattribution
    §3 converts the ``EWOULDBLOCK`` interleaving away from.

    Raises:
        OSError: ``ENOENT`` when the partial no longer has a directory entry.
    """
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
    """Whether ``dest`` already holds a good base, checked just before this fetcher would publish.

    Deliberately **not** :func:`_staged_base_rejection`, despite asking the same question of the
    same file. That one raises on an unreadable ``dest`` because a cache miss there would trigger
    a silent, perpetual multi-GiB re-download. Here the polarity is reversed: a ``False`` costs one
    ``os.replace`` — the behavior before this guard existed, and one that *repairs* an unreadable
    ``dest``, since a rename needs permission on the directory rather than on the file. So every
    ``OSError`` is answered "publish", and this can never add a failure to a download that already
    succeeded.

    Answering "publish" on an *unreadable* ``dest`` is a deliberate trade, not an oversight: the
    alternative — skip, and hand back a base this process could not evaluate at all — is worse than
    the orphaned inode it avoids, when a verified copy of the same content-addressed bytes is
    already in hand. ``EACCES`` is the case the rename repairs outright. ``EMFILE`` cannot reach the
    publish anyway, because :func:`_durable_replace` needs a descriptor of its own and fails first.
    That leaves a transient ``EIO``, where publishing costs at most ADR-0443 §2's already-accepted
    inode orphan; ADR-0446 §7 records it as a residue rather than claiming the gate is airtight.

    **The completion marker is required here too** (ADR-0451 §4). Under the reuse gate a marker-less
    base is one every future fetch will *reject*, so skipping the publish on one would hand the
    investigation a base that is re-downloaded and re-rejected for as long as it lives — trading a
    bounded orphan inode for an unbounded re-download loop, which is the wrong direction.

    **That branch is not rare, and it must not be described as if it were.** Beside the
    died-mid-publish sibling it is also the *deterministic* first re-provision of every base staged
    before ADR-0451: such a base is present and magic-passing and has no marker, so the re-stage
    ``os.replace``\\ s over it. ADR-0443 §2 already accepts the residue that follows — the
    superseded inode survives with zero links while some QEMU holds it open as a backing file,
    charged to ``df`` and matching no path — but accepted it as a rare race, and the upgrade turns
    it into one
    occurrence per (investigation, token) whose base a running guest holds. Bounded by that guest's
    lifetime and recorded in ADR-0451's Consequences with the free-space cost that accompanies it;
    do not re-derive it as a sub-millisecond window.

    Adopting the marker-less base instead — writing a marker for it rather than re-staging — would
    avoid both costs and is rejected in ADR-0451, because a marker-less base is exactly the file
    nothing can distinguish between "staged fine by older code" and "torn by the crash #1539 is
    about". Marking it complete would be asserting the very thing that cannot be checked.
    """
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
    """Publish the verified partial onto ``dest`` and mark it complete, durably (ADR-0443/0451).

    ``os.replace`` is atomic with respect to concurrent *readers*, not with respect to a host crash
    or power loss: on a default ext4 mount the rename can become durable while the data blocks
    behind it are not, leaving a full-length ``dest`` of zeros or stale blocks (#1526). The file
    sync closes that window, in the flush → ``fsync`` → ``os.replace`` shape
    ``inventory/writeback.py`` already uses for the systems TOML.

    Syncing **here** rather than as each stager closes its writer is what keeps the cost on the
    published bases only: every verification gate — each stager's checksum and the shared
    qcow2-magic gate — has already run and raised by the time this is called, so a partial the
    ``finally`` is about to discard never costs a full flush of a base up to the 50 GiB canonical
    cap. It also leaves durability at the single publish point instead of once per codec.

    Without the directory sync the *rename* can be lost on a crash even though the data behind it
    survived. That alone is benign — an absent ``dest`` is re-staged — but the same directory entry
    carries the partial's unlink, so a lost rename can resurrect the partial as an orphan. Both
    halves are made durable together, at the cost of one metadata sync per staged base.

    **The completion marker is written last and any stale one removed first** (ADR-0451 §2). The
    ordering is the whole guarantee, so it is stated as one sequence rather than left to a reader:

    1. ``fsync`` the partial — the base's data is durable.
    2. unlink any stale marker, and
    3. ``fsync`` the directory, so its *absence* is durable before the rename.
    4. ``os.replace`` publishes.
    5. ``fsync`` the directory — the rename and the partial's unlink are durable.
    6. create and ``fsync`` the marker, and
    7. ``fsync`` the directory so the marker's link is durable.

    Every crash point leaves one of two states, and both are correct: **no marker**, so the next
    fetch re-stages; or a **marker over a base whose data was made durable at step 1**, so the next
    fetch reuses it. Steps 2–3 are the re-stage case, and they are not decoration: the token is a
    content address, so a re-stage can begin with a marker already present, and without them the
    recovered state could be the *previous* base under a marker attesting to one this pass had
    already rejected. Ordering the removal's durability ahead of the rename removes that state
    outright rather than reasoning about a particular filesystem's metadata ordering — the derived
    invariant ADR-0446 and ADR-0452 exist to delete.
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
