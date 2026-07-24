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

Concurrency (ADR-0441 §5): the shared per-(investigation, checksum) staging path means two sibling
Systems can provision at once. Each fetcher writes a **unique** ``<token>.<uuid>.partial`` and
``os.replace``s it onto ``<token>.qcow2`` only after verify, so no two downloaders share a
partial — the correctness guarantee. A **session-scoped** ``pg_advisory_lock`` (keyed via
``db.locks._session_lock_key``, held on this call's dedicated sync connection across the download)
collapses the redundant multi-GiB download; while it is held — so a live sibling partial is not
expected — a crash-orphaned ``<token>.*.partial`` is glob-unlinked opportunistically. A lock lost
mid-transfer breaks that expectation; see :func:`_unlink_orphan_partials`.
"""

from __future__ import annotations

import base64
import hashlib
import os
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import psycopg

import kdive.config as config
from kdive.artifacts import storage as artifact_types
from kdive.artifacts.content_address import rootfs_object_name, rootfs_object_token
from kdive.artifacts.transport_encoding import (
    GZIP_ENCODING,
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
from kdive.store.objectstore import artifact_key, object_store_from_env

_TENANT = "local"
_OWNER_KIND = "investigations"
# The qcow2 magic every canonical rootfs base must start with (bytes ``51 46 49 fb``); a base that
# does not is rejected here rather than failing late and confusingly at ``qemu-img`` (ADR-0438).
_QCOW2_MAGIC = b"QFI\xfb"
# The identity path's per-read window: the staging loop hashes and writes one chunk at a time, so
# peak memory is this constant rather than the object size (up to the 5 GiB single-PUT ceiling).
# Matches the gzip path's ranged-read window in ``transport_encoding``.
_STREAM_CHUNK_BYTES = 4 * 1024 * 1024


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
    ``advisory_xact_lock`` would trip ``idle_in_transaction_session_timeout``). A present verified
    staged file is reused. S3 is a required backend (ADR-0337).
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
    verified staged file, and otherwise downloads it once under a session advisory lock.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the System has no investigation binding, the
            checksum is not owned by the investigation, the object was never uploaded, or the
            canonical base is not a qcow2; ``INFRASTRUCTURE_FAILURE`` on a missing/mismatched
            checksum or a staging IO fault.
    """
    token = rootfs_object_token(upload.checksum_sha256)
    investigation_id = _resolve_investigation(conn, upload.system_id)
    resolved = _resolve_object(conn, investigation_id, token, upload)
    dest = staged_rootfs_path(investigation_id, token, upload_dir=upload.upload_dir)
    if dest.is_file():
        return dest
    lock_key = _session_lock_key(_fetch_lock_name(investigation_id, token))
    conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
    try:
        if dest.is_file():  # a sibling fetcher finished while we waited on the lock
            return dest
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
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
    return dest


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
    """Glob-unlink a crash-orphaned ``<token>.*.partial`` under the fetch lock (ADR-0441 §5).

    Runs only while holding the fetch lock, which serializes downloads, so a *live* sibling partial
    is not expected — a match is normally a killed worker's SENSITIVE orphan, bounded by this next
    fetch rather than by full investigation reclaim.

    That premise holds only while the lock does. A lock lost mid-transfer (an idle-session timeout,
    a recycled connection) lets this sweep unlink a *live* sibling's partial: that fetcher writes on
    into the unlinked inode and fails at ``os.replace`` — a failed provision, never a corrupt
    ``dest`` — while those blocks stay charged to ``df`` yet invisible to every path-matching tool,
    so the disk pressure looks like a leak the sweeps missed. Bounding this glob away from live
    partials is #1524; do not widen it (outside the lock, or over the whole uploads dir) on the
    strength of the paragraph above.
    """
    with suppress(OSError):
        for orphan in dest.parent.glob(f"{dest.stem}.*.partial"):
            orphan.unlink(missing_ok=True)


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

    HEAD the object (absent → ``CONFIGURATION_ERROR``; no stored checksum →
    ``INFRASTRUCTURE_FAILURE``). When ``encoding`` is ``gzip`` the object is streamed-decompressed
    (bounded by ``uncompressed_size``, gzip-bomb guarded, transport-hash verified); otherwise it is
    streamed verbatim and its SHA-256 verified. Neither path buffers the whole object. Either way
    the canonical base is qcow2-magic-validated and written atomically (a ``<token>.<uuid>.partial``
    temp + ``os.replace``) so ``dest`` is only ever a verified base and two concurrent fetchers
    never share a partial. The partial is unlinked in a ``finally``, so no failure — typed or not —
    leaves one behind.
    """
    head = store.head(object_key)
    if head is None:
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
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if effective is None:
            _stage_identity(
                store,
                key=object_key,
                checksum=head.checksum_sha256,
                partial=partial,
                system_id=system_id,
            )
        elif effective == GZIP_ENCODING:
            _stage_gzip(
                store,
                key=object_key,
                compressed_size=head.size_bytes,
                checksum=head.checksum_sha256,
                uncompressed_size=uncompressed_size,
                partial=partial,
                system_id=system_id,
            )
        else:
            # Defence in depth: the declaration validator (ADR-0437) rejects an unknown codec, so
            # this is unreachable with valid data — but naming the codec beats silently staging it
            # as identity and failing with a misleading "not a qcow2" magic error.
            raise CategorizedError(
                f"uploaded rootfs declared an unsupported transport encoding {effective!r}; "
                "only gzip is supported",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id)},
            )
        _require_qcow2_magic(partial, system_id=str(system_id))
        os.replace(partial, dest)
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
    partial: Path,
    system_id: UUID,
) -> None:
    """Stream an unencoded upload verbatim into the partial, verifying its SHA-256.

    The object is read in ``_STREAM_CHUNK_BYTES`` windows off a single unconditional GET (``etag``
    is ``None``, ADR-0054: the provision plane holds no client handle), each chunk hashed and
    written as it arrives, so peak memory is the chunk rather than the object — a multi-GiB rootfs
    no longer spikes the worker (#1520). A read may return fewer bytes than asked without being
    end-of-stream (the store's body wrapper is a ``RawIOBase``); only a true empty read ends the
    loop.

    The checksum is verified here, before the caller's shared qcow2-magic gate, which is the order
    ADR-0438 §3 and ADR-0441 §5 specify: a mismatch keeps ADR-0434 §2's ``INFRASTRUCTURE_FAILURE``
    rather than being reported by whichever gate a corrupt object happens to trip first. (Whether
    that category is the right one is unsettled — the gzip path raises ``CONFIGURATION_ERROR`` for
    the byte-identical failure — and is tracked in #1523.)
    """
    hasher = hashlib.sha256()
    with store.get_artifact_stream(key, None) as fetched, partial.open("wb") as writer:
        while chunk := fetched.reader.read(_STREAM_CHUNK_BYTES):
            hasher.update(chunk)
            writer.write(chunk)
    if base64.b64encode(hasher.digest()).decode("ascii") != checksum:
        raise CategorizedError(
            "uploaded rootfs object failed checksum verification",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )


def _stage_gzip(
    store: UploadObjectStore,
    *,
    key: str,
    compressed_size: int,
    checksum: str,
    uncompressed_size: int | None,
    partial: Path,
    system_id: UUID,
) -> None:
    """Stream-gunzip a gzip transport object to the partial, bounded and transport-hash verified."""
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
    with partial.open("wb") as writer:
        strip_gzip_to_writer(store, request, writer)


def _require_qcow2_magic(staged: Path, *, system_id: str) -> None:
    """Reject a staged base that does not start with the qcow2 magic (ADR-0438).

    Reads the finished ``.partial``, so both codecs are gated by one mechanism over the canonical
    bytes — each stager has already verified its own checksum, and a base too short to hold the
    magic yields a short read that fails here rather than staging unchecked.
    """
    with staged.open("rb") as reader:
        first_bytes = reader.read(len(_QCOW2_MAGIC))
    if first_bytes != _QCOW2_MAGIC:
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
