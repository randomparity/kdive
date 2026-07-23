"""Synchronous upload-rootfs fetch for the local-libvirt provision lane (ADR-0434).

Wires the previously no-op ``upload`` rootfs lane: a System-owned uploaded qcow2 (ADR-0048 §5)
is downloaded from the object store to a checksum-verified local path at provision time. Mirrors
``rootfs_catalog_fetch_from_env`` — a synchronous callable that lazily opens its object store per
call, because the provider provision seam runs off the event loop (``asyncio.to_thread``) and
owns no async pool. Unlike the catalog fetch it needs **no DB connection**: the uploaded object
carries its own integrity anchor (the base64 SHA-256 signed into the presigned PUT, ADR-0048 §2,
read back via ``head().checksum_sha256``).
"""

from __future__ import annotations

import base64
import hashlib
import os
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from kdive.artifacts import storage as artifact_types
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    UploadFetch,
    upload_rootfs_path,
)
from kdive.store.objectstore import artifact_key, object_store_from_env


class UploadObjectStore(Protocol):
    """The narrow object-store capability the upload fetch needs (an :class:`ObjectStore`)."""

    def head(self, key: str) -> artifact_types.HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...


def _sha256_b64(data: bytes) -> str:
    """Return the base64-encoded SHA-256 of ``data`` (the object-store checksum format)."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def rootfs_upload_fetch_from_env() -> UploadFetch:
    """A synchronous ``(RootfsUploadContext) -> Path`` uploaded-rootfs fetch (ADR-0434).

    Builds the object store lazily per call (the provision seam runs in a thread and owns no
    async pool). A present staged file is reused (it was written only after passing the checksum,
    so it is a verified base); otherwise the object is HEADed for its stored checksum, downloaded,
    verified, and atomically staged. S3 is a required backend (ADR-0337).
    """

    def _fetch(upload: RootfsUploadContext) -> Path:
        return fetch_uploaded_rootfs(object_store_from_env(), upload)

    return _fetch


def fetch_uploaded_rootfs(store: UploadObjectStore, upload: RootfsUploadContext) -> Path:
    """Download + checksum-verify the System-owned uploaded rootfs to a local path (ADR-0434).

    A present staged file is reused. Otherwise: HEAD the object (absent → ``CONFIGURATION_ERROR``;
    no stored checksum → ``INFRASTRUCTURE_FAILURE``), download it, verify its SHA-256 against the
    stored checksum, and write it atomically (``.partial`` temp + ``os.replace``) so ``dest`` is
    only ever a verified base.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the object was never uploaded;
            ``INFRASTRUCTURE_FAILURE`` on a missing/mismatched checksum or a staging IO fault.
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

    data = store.get_artifact(key, None).data
    if _sha256_b64(data) != head.checksum_sha256:
        raise CategorizedError(
            "uploaded rootfs object failed checksum verification",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(upload.system_id)},
        )
    _atomic_write(dest, data, system_id=str(upload.system_id))
    return dest


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
