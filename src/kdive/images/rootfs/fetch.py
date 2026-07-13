"""Fetch a registered catalog rootfs object to a checksum-verified local cache (ADR-0092).

This wires what was a `not wired yet` stub: object-store-backed rootfs materialization. The
resolver returns a registered row; this downloads its ``object_key``, verifies the content
SHA-256 against the row's ``digest``, and caches it locally keyed by digest so a repeat boot of
the same image reuses the bytes. The object GET is offloaded via ``asyncio.to_thread`` (boto3 is
synchronous).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts import storage as artifact_types
from kdive.components.local_paths import validate_local_component_path
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.catalog import resolve_public_rootfs_sync, resolve_rootfs

_SHA256_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class RootfsObjectStore(Protocol):
    """The narrow object-store capability the rootfs fetch needs (an :class:`ObjectStore`)."""

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...


def _cache_path(cache_dir: Path, digest: str) -> Path:
    """A digest-keyed cache path so a repeat boot of the same image reuses the bytes.

    The digest is validated as ``sha256:<64 lowercase hex>`` before it forms a filename, so a
    malformed row value can never escape ``cache_dir`` (defense-in-depth path-traversal guard).
    """
    if not _SHA256_DIGEST.match(digest):
        raise CategorizedError(
            "rootfs catalog digest is not a sha256:<hex> value",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"digest": digest},
        )
    return cache_dir / f"{digest.removeprefix('sha256:')}.qcow2"


def _cache_io_error(
    *,
    provider: str,
    name: str,
    object_key: str,
    cache_path: Path,
    err: OSError,
) -> CategorizedError:
    return CategorizedError(
        f"failed to write registered rootfs cache path {str(cache_path)!r}: {err.strerror}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={
            "provider": provider,
            "name": name,
            "object_key": object_key,
            "cache_path": str(cache_path),
        },
    )


def _unlink_tmp_cache(tmp: Path) -> None:
    with suppress(OSError):
        tmp.unlink()


def _required_object_ref(
    object_key: str | None, digest: str | None, *, provider: str, name: str
) -> tuple[str, str]:
    if object_key is None or digest is None:
        raise CategorizedError(
            "registered rootfs row is missing its object_key or digest",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name},
        )
    return object_key, digest


def _materialize_s3_rootfs(
    fetch_data: Callable[[], bytes],
    *,
    provider: str,
    name: str,
    object_key: str,
    digest: str,
    cache_dir: Path,
) -> Path:
    cached = _cache_path(cache_dir, digest)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if cached.is_file():
            return cached
    except OSError as err:
        raise _cache_io_error(
            provider=provider,
            name=name,
            object_key=object_key,
            cache_path=cached,
            err=err,
        ) from err

    data = fetch_data()
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise CategorizedError(
            "fetched rootfs object digest does not match the catalog row",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name, "object_key": object_key},
        )

    tmp = cached.with_suffix(".qcow2.partial")
    try:
        tmp.write_bytes(data)
        tmp.replace(cached)
    except OSError as err:
        _unlink_tmp_cache(tmp)
        raise _cache_io_error(
            provider=provider,
            name=name,
            object_key=object_key,
            cache_path=cached,
            err=err,
        ) from err
    return cached


async def fetch_registered_rootfs(
    conn: AsyncConnection,
    store: RootfsObjectStore,
    *,
    provider: str,
    name: str,
    project: str,
    cache_dir: Path,
) -> Path:
    """Resolve a registered rootfs row and return a checksum-verified local cache path.

    Resolves the registered image visible to ``project`` (private shadows public), downloads its
    ``object_key``, verifies the content SHA-256 against the row's ``digest``, and writes it to a
    digest-keyed file under ``cache_dir`` (reused on a cache hit).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no registered image resolves;
            ``INFRASTRUCTURE_FAILURE`` when the downloaded bytes do not match the row's digest.
    """
    row = await resolve_rootfs(conn, provider, name, project=project)
    if row is None:
        raise CategorizedError(
            "unknown registered rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name},
        )
    # A registered row always has an object_key and a digest (the DB CHECK and the publish path
    # guarantee it), so both are present here.
    object_key, digest = _required_object_ref(
        row.object_key, row.digest, provider=provider, name=name
    )
    return await asyncio.to_thread(
        _materialize_s3_rootfs,
        lambda: store.get_artifact(object_key, None).data,
        provider=provider,
        name=name,
        object_key=object_key,
        digest=digest,
        cache_dir=cache_dir,
    )


def fetch_registered_rootfs_sync(
    conn: psycopg.Connection,
    store_factory: Callable[[], RootfsObjectStore],
    *,
    allowed_roots: list[Path],
    provider: str,
    name: str,
    arch: str,
    cache_dir: Path,
) -> Path:
    """Resolve a registered public rootfs and return a provider-readable local path (sync).

    The synchronous twin of :func:`fetch_registered_rootfs` for the local-libvirt provision seam,
    which runs off the event loop and owns no async pool (ADR-0228). Resolves the registered public
    image of ``arch`` and branches on the source column:

    - a **staged-path** row resolves to its ``path`` validated against ``allowed_roots``
      (``validate_local_component_path`` re-checks absolute-ness, existence, containment incl.
      symlink escape, regular-file, and readability) — **no object store, no cache, no digest**;
      ``store_factory`` is never called, because staged-path resolves from a host-local file and
      never touches the object store — a cost optimization that avoids round-tripping a multi-GB
      rootfs through S3 (S3 is a required backend, ADR-0337).
    - an **s3** row builds the store via ``store_factory``, downloads ``object_key``, verifies its
      sha256 against ``digest``, and caches it under a digest-keyed file in ``cache_dir``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no registered public image of ``arch``
            resolves or a staged path fails validation; ``INFRASTRUCTURE_FAILURE`` on a digest
            mismatch or a cache IO fault.
    """
    row = resolve_public_rootfs_sync(conn, provider, name, arch)
    if row is None:
        raise CategorizedError(
            "unknown registered rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name, "arch": arch},
        )
    if row.path is not None:
        return validate_local_component_path(row.path, allowed_roots=allowed_roots)
    object_key, digest = _required_object_ref(
        row.object_key, row.digest, provider=provider, name=name
    )
    return _materialize_s3_rootfs(
        lambda: store_factory().get_artifact(object_key, None).data,
        provider=provider,
        name=name,
        object_key=object_key,
        digest=digest,
        cache_dir=cache_dir,
    )
