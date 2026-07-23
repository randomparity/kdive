"""Local-libvirt component materialization (ADR-0065, ADR-0092).

A ``catalog`` rootfs reference resolves through the DB-backed ``image_catalog`` and its object is
fetched to a checksum-verified local cache (the cutover from the read-only YAML lookup). The
resolve+fetch capability is injected as ``RootfsMaterializationContext.catalog_fetch`` because
the provider provision seam is synchronous and owns no Postgres connection; the worker wires a
concrete fetch (a connection + object store) into the context. The ``local`` and ``upload`` paths
are unchanged provider-local resolutions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.components.local_paths import validate_local_component_path
from kdive.components.references import (
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs

# Resolve a `catalog` reference (for a target arch) to a provider-readable local path. A staged-path
# row resolves to its host path; an s3 row resolves DB row → object → cache (ADR-0092/0228).
type CatalogFetch = Callable[[CatalogComponentRef, str], Path]
# Download + checksum-verify a System-owned uploaded rootfs object to a provider-readable local
# path (ADR-0434). Injected like ``CatalogFetch`` because the provider provision seam is
# synchronous and owns no object store; the worker wires a concrete fetch.
type UploadFetch = Callable[["RootfsUploadContext"], Path]
type MaterializableRootfsRef = LocalComponentRef | CatalogComponentRef | _UploadRootfs


@dataclass(frozen=True, slots=True)
class RootfsUploadContext:
    """System-owned upload staging context for an uploaded rootfs."""

    tenant: str
    system_id: UUID
    upload_dir: Path


@dataclass(frozen=True, slots=True)
class RootfsMaterializationContext:
    """Inputs needed to resolve a provider-readable rootfs base path.

    ``catalog_fetch`` resolves a ``catalog`` reference through ``image_catalog`` (a staged-path row
    to its host path, an s3 row downloaded to a checksum-verified cache); it is ``None`` in lanes
    that never resolve a catalog reference (then a ``catalog`` reference is a configuration error).
    ``arch`` is the provisioning profile's target arch, threaded into ``catalog_fetch`` so a
    same-name multi-arch image resolves deterministically (ADR-0228); it is unused on the
    ``local``/``upload`` lanes.

    ``upload_fetch`` downloads and checksum-verifies a System-owned uploaded rootfs object to a
    local path (ADR-0434); it is ``None`` in lanes that never resolve an ``upload`` reference
    (then an ``upload`` reference is a configuration error), mirroring ``catalog_fetch``.
    """

    allowed_roots: list[Path]
    arch: str = "x86_64"
    upload: RootfsUploadContext | None = None
    catalog_fetch: CatalogFetch | None = None
    upload_fetch: UploadFetch | None = None


def materialize_rootfs_base(
    ref: MaterializableRootfsRef,
    *,
    context: RootfsMaterializationContext,
) -> Path:
    """Return a provider-readable rootfs base image path."""
    if isinstance(ref, _UploadRootfs):
        return _materialize_uploaded_rootfs(context)
    if isinstance(ref, LocalComponentRef):
        return _materialize_local_rootfs(ref, context)
    if isinstance(ref, CatalogComponentRef):
        return _materialize_catalog_rootfs(ref, context)
    raise CategorizedError(
        "unsupported rootfs component reference",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def upload_rootfs_path(tenant: str, system_id: UUID | str, *, upload_dir: Path) -> Path:
    """Return the local staging path for a System-owned uploaded rootfs object.

    ``system_id`` accepts a ``str`` (like ``overlay_path`` / ``baseline_dir``) so a teardown
    that only holds the domain name can reconstruct the path.
    """
    return upload_dir / f"{tenant}-systems-{system_id}-rootfs.qcow2"


def _materialize_uploaded_rootfs(context: RootfsMaterializationContext) -> Path:
    """Download + checksum-verify the System-owned uploaded rootfs to a local path (ADR-0434).

    The download is injected (``context.upload_fetch``) so the synchronous provider seam stays
    connectionless; an unwired lane treats an ``upload`` reference as a configuration error.
    """
    if context.upload is None:
        raise CategorizedError(
            "uploaded rootfs materialization requires upload context",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if context.upload_fetch is None:
        raise CategorizedError(
            "upload rootfs materialization is not wired for this lane",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return context.upload_fetch(context.upload)


def _materialize_local_rootfs(
    ref: LocalComponentRef, context: RootfsMaterializationContext
) -> Path:
    return validate_local_component_path(
        ref.path,
        allowed_roots=context.allowed_roots,
        sha256=ref.sha256,
    )


def _materialize_catalog_rootfs(
    ref: CatalogComponentRef, context: RootfsMaterializationContext
) -> Path:
    """Resolve a ``catalog`` rootfs through the DB catalog and fetch its object to a cache.

    The resolve+fetch is injected (``context.catalog_fetch``) so the synchronous provider seam
    stays connectionless; an unwired lane treats a ``catalog`` reference as a configuration error.
    """
    if context.catalog_fetch is None:
        raise CategorizedError(
            "catalog rootfs materialization is not wired for this lane",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": ref.provider, "name": ref.name},
        )
    return context.catalog_fetch(ref, context.arch)
