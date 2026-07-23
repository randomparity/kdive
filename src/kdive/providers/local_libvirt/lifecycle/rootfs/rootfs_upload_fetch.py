"""Synchronous upload-rootfs fetch for the local-libvirt provision lane (ADR-0434, ADR-0438).

Wires the ``upload`` rootfs lane: a System-owned uploaded qcow2 (ADR-0048 §5) is downloaded from the
object store to a checksum-verified local path at provision time. Mirrors
``rootfs_catalog_fetch_from_env`` — a synchronous callable that lazily opens its resources per call,
because the provider provision seam runs off the event loop (``asyncio.to_thread``) and owns no
async pool.

ADR-0438 extends it: the declared transport ``encoding`` (a DB-manifest fact — the presign stamps no
``content-encoding``) is read via a short-lived sync connection, exactly as the ADR-0228 catalog
fetch reads its catalog row. A ``gzip`` upload is streamed-decompressed to the staged base (never
buffering the multi-GiB canonical object) via the shared ``strip_gzip_to_writer``; an identity
upload stages verbatim. Either way the canonical base is qcow2-magic-validated before it backs the
overlay, closing the rootfs no-format-validation gap for the upload path (catalog images are
pre-vetted).
"""

from __future__ import annotations

import base64
import hashlib
import os
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID

import psycopg

import kdive.config as config
from kdive.artifacts import storage as artifact_types
from kdive.artifacts.transport_encoding import (
    GZIP_ENCODING,
    StripDecodeRequest,
    normalize_encoding,
    strip_gzip_to_writer,
)
from kdive.artifacts.upload_manifest import get_manifest_sync
from kdive.config.core_settings import DATABASE_URL
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    UploadFetch,
    upload_rootfs_path,
)
from kdive.store.objectstore import artifact_key, object_store_from_env

# The uploaded rootfs manifest entry name (systems accept a single ``rootfs`` artifact). The stored
# object key is ``artifact_key(tenant, "systems", <id>, "rootfs")``, so the manifest entry that
# carries this object's declared encoding is likewise named ``rootfs``.
_ROOTFS_ENTRY_NAME = "rootfs"
# The qcow2 magic every canonical rootfs base must start with (bytes ``51 46 49 fb``); a base that
# does not is rejected here rather than failing late and confusingly at ``qemu-img`` (ADR-0438).
_QCOW2_MAGIC = b"QFI\xfb"


class UploadObjectStore(Protocol):
    """The narrow object-store capability the upload fetch needs (an :class:`ObjectStore`).

    ``get_range`` widens it to satisfy :class:`transport_encoding.RangedReadStore`, so a gzip upload
    can be streamed-decompressed without a whole-object buffer.
    """

    def head(self, key: str) -> artifact_types.HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


def _sha256_b64(data: bytes) -> str:
    """Return the base64-encoded SHA-256 of ``data`` (the object-store checksum format)."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def read_rootfs_upload_encoding(
    conn: psycopg.Connection, system_id: UUID
) -> tuple[str | None, int | None]:
    """Read the declared ``(encoding, uncompressed_size)`` for a System's uploaded rootfs.

    Reads the systems upload manifest's ``rootfs`` entry. A missing manifest or missing entry (e.g.
    reaped after its deadline) returns ``(None, None)`` — the identity fallback, i.e. today's
    verbatim behavior; a stray gzip whose manifest was reaped then fails closed at the qcow2 magic
    check rather than being staged as an invalid qcow2.

    Args:
        conn: A sync connection.
        system_id: The owning System's id.

    Returns:
        ``(encoding, uncompressed_size)`` — ``encoding`` normalized so identity is ``None``.
    """
    manifest = get_manifest_sync(conn, "systems", system_id)
    if manifest is None:
        return (None, None)
    for entry in manifest.entries:
        if entry.name == _ROOTFS_ENTRY_NAME:
            return (normalize_encoding(entry.encoding), entry.uncompressed_size)
    return (None, None)


def rootfs_upload_fetch_from_env() -> UploadFetch:
    """A synchronous ``(RootfsUploadContext) -> Path`` uploaded-rootfs fetch (ADR-0434, ADR-0438).

    Opens a short-lived sync ``psycopg`` connection per call to read the declared transport encoding
    from the upload manifest (the provision seam runs in a thread and owns no async pool; the
    catalog fetch, ADR-0228, opens its own sync connection the same way), then builds the object
    store lazily. A present staged file is reused (it was written only after passing checksum +
    magic, so it is a verified base). S3 is a required backend (ADR-0337).
    """

    def _fetch(upload: RootfsUploadContext) -> Path:
        with psycopg.connect(config.require(DATABASE_URL)) as conn:
            encoding, uncompressed_size = read_rootfs_upload_encoding(conn, upload.system_id)
        return fetch_uploaded_rootfs(
            object_store_from_env(),
            upload,
            encoding=encoding,
            uncompressed_size=uncompressed_size,
        )

    return _fetch


def fetch_uploaded_rootfs(
    store: UploadObjectStore,
    upload: RootfsUploadContext,
    *,
    encoding: str | None = None,
    uncompressed_size: int | None = None,
) -> Path:
    """Download the System-owned uploaded rootfs to a checksum- and format-verified local path.

    A present staged file is reused. Otherwise: HEAD the object (absent → ``CONFIGURATION_ERROR``;
    no stored checksum → ``INFRASTRUCTURE_FAILURE``). When ``encoding`` is ``gzip`` the compressed
    object is streamed-decompressed to the staged base (bounded by ``uncompressed_size``, gzip-bomb
    guarded, transport-hash verified); otherwise the object is downloaded and its SHA-256 verified
    against the stored checksum. Either way the canonical base is qcow2-magic-validated and written
    atomically (``.partial`` temp + ``os.replace``) so ``dest`` is only ever a verified base.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the object was never uploaded, a gzip
            declared without ``uncompressed_size``, a transport-hash mismatch or gzip bomb, or a
            non-qcow2 canonical base; ``INFRASTRUCTURE_FAILURE`` on a missing/mismatched checksum or
            a staging IO fault.
    """
    dest = upload_rootfs_path(upload.tenant, upload.system_id, upload_dir=upload.upload_dir)
    if dest.is_file():
        return dest

    key = artifact_key(upload.tenant, "systems", str(upload.system_id), "rootfs")
    head = store.head(key)
    if head is None:
        raise CategorizedError(
            "upload-kind rootfs was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(upload.system_id)},
        )
    if head.checksum_sha256 is None:
        raise CategorizedError(
            "uploaded rootfs object has no stored checksum; re-upload via the presigned PUT",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(upload.system_id)},
        )

    if normalize_encoding(encoding) == GZIP_ENCODING:
        _stage_gzip(
            store,
            upload,
            key=key,
            compressed_size=head.size_bytes,
            checksum=head.checksum_sha256,
            uncompressed_size=uncompressed_size,
            dest=dest,
        )
    else:
        _stage_identity(store, upload, key=key, checksum=head.checksum_sha256, dest=dest)
    return dest


def _stage_identity(
    store: UploadObjectStore,
    upload: RootfsUploadContext,
    *,
    key: str,
    checksum: str,
    dest: Path,
) -> None:
    """Stage an unencoded upload verbatim: verify the checksum, magic-check, atomic-write."""
    data = store.get_artifact(key, None).data
    if _sha256_b64(data) != checksum:
        raise CategorizedError(
            "uploaded rootfs object failed checksum verification",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(upload.system_id)},
        )
    _require_qcow2_magic(data[:4], system_id=str(upload.system_id))
    _atomic_write(dest, data, system_id=str(upload.system_id))


def _stage_gzip(
    store: UploadObjectStore,
    upload: RootfsUploadContext,
    *,
    key: str,
    compressed_size: int,
    checksum: str,
    uncompressed_size: int | None,
    dest: Path,
) -> None:
    """Stream-gunzip a gzip transport object to ``dest``, bounded, hash- and magic-verified."""
    if uncompressed_size is None:
        raise CategorizedError(
            "uploaded rootfs declared a gzip encoding without an uncompressed_size; re-declare the "
            "upload with the canonical object size",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(upload.system_id)},
        )
    request = StripDecodeRequest(
        key=key,
        compressed_size=compressed_size,
        expected_sha256=checksum,
        uncompressed_size=uncompressed_size,
    )
    tmp = dest.with_suffix(".qcow2.partial")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as writer:
            strip_gzip_to_writer(store, request, writer)
        with tmp.open("rb") as reader:
            _require_qcow2_magic(reader.read(4), system_id=str(upload.system_id))
        os.replace(tmp, dest)
    except OSError as err:
        with suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            f"failed to stage the uploaded rootfs to {str(dest)!r}: {err.strerror}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(upload.system_id), "dest": str(dest)},
        ) from err
    except CategorizedError:
        with suppress(OSError):
            tmp.unlink()
        raise


def _require_qcow2_magic(first_bytes: bytes, *, system_id: str) -> None:
    """Reject a canonical base that does not start with the qcow2 magic (ADR-0438)."""
    if first_bytes[:4] != _QCOW2_MAGIC:
        raise CategorizedError(
            "staged rootfs is not a qcow2 image: the uploaded object (after any transport decode) "
            "does not start with the qcow2 magic; upload a qcow2 image",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": system_id},
        )


def _atomic_write(dest: Path, data: bytes, *, system_id: str) -> None:
    tmp = dest.with_suffix(".qcow2.partial")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError as err:
        with suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            f"failed to stage the uploaded rootfs to {str(dest)!r}: {err.strerror}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": system_id, "dest": str(dest)},
        ) from err
