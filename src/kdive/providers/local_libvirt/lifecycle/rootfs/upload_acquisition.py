"""Resolve uploaded-rootfs objects and coordinate investigation-scoped fetch leases."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

# The function rather than the module, so a test's monkeypatch is scoped to this module instead of
# replacing ``shutil.disk_usage`` process-wide for every other importer for the test's duration.
from uuid import UUID

import psycopg

import kdive.config as config
from kdive.artifacts.uploads.content_address import rootfs_object_name, rootfs_object_token
from kdive.artifacts.uploads.transport_encoding import (
    normalize_encoding,
)
from kdive.config.core_settings import DATABASE_URL
from kdive.db.locks import _session_lock_key
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    UploadFetch,
    staged_rootfs_path,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.upload_contracts import UploadObjectStore
from kdive.providers.local_libvirt.lifecycle.rootfs.upload_publication import (
    _REJECTION_PROSE,
    _staged_base_rejection,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.upload_staging import (
    _unlink_orphan_partials,
    stage_uploaded_rootfs,
)
from kdive.providers.shared.staging.rootfs_fetch_leases import (
    acquire_fetch_lease,
    release_fetch_lease,
)
from kdive.store.objectstore import artifact_key

_log = logging.getLogger(__name__)

_TENANT = "local"
_OWNER_KIND = "investigations"


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


def rootfs_upload_fetch_from_env(store: UploadObjectStore) -> UploadFetch:
    """A synchronous ``(RootfsUploadContext) -> Path`` uploaded-rootfs fetch (ADR-0441).

    Closes over the process-assembled object store and opens a short-lived **autocommit** sync
    ``psycopg`` connection per call to resolve the System's investigation and committed object.
    The provision seam runs in a thread and owns no async pool; the catalog fetch, ADR-0228, opens
    its own sync connection the same way. Autocommit ensures the session advisory lock held across
    the multi-GiB download never keeps a transaction open (an ``advisory_xact_lock`` would trip
    ``idle_in_transaction_session_timeout``). A present staged file is reused once it re-passes the
    format gate (ADR-0443). S3 is a required backend (ADR-0337).
    """

    def _fetch(upload: RootfsUploadContext) -> Path:
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
