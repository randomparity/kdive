"""Base-image acquirer for the local rootfs catalog (ADR-0251).

A catalog row's :class:`~kdive.images.rootfs_catalog.RootfsSource` is materialized into a
``scratch`` qcow2 by either invoking ``virt-builder`` against a template or downloading a
sha256-pinned cloud image and verifying its digest. Both paths fail closed: a digest mismatch or
an unreachable URL raises a ``CONFIGURATION_ERROR`` rather than handing a corrupt base downstream.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs_catalog import CloudImageSource, RootfsSource, VirtBuilderSource

type Downloader = Callable[[str, Path], None]

_DIGEST_CHUNK_BYTES = 1 << 20


def _real_download(url: str, dest: Path) -> None:  # pragma: no cover - network IO
    """Stream ``url`` to ``dest`` using ``urllib`` (the default, un-unit-tested downloader)."""
    with urllib.request.urlopen(url) as response, dest.open("wb") as handle:  # noqa: S310
        while chunk := response.read(_DIGEST_CHUNK_BYTES):
            handle.write(chunk)


def _sha256_of(path: Path) -> str:
    """Compute the sha256 of ``path`` with a streaming read."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _acquire_cloud_image(source: CloudImageSource, scratch: Path, downloader: Downloader) -> None:
    """Download ``source`` to ``scratch`` and verify its pinned sha256, failing closed."""
    try:
        downloader(source.url, scratch)
    except (urllib.error.URLError, OSError) as exc:
        raise CategorizedError(
            "cloud base image is unreachable",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "base_unreachable", "url": source.url},
        ) from exc
    actual = _sha256_of(scratch)
    if actual != source.sha256:
        raise CategorizedError(
            "cloud base image sha256 does not match the catalog pin",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "base_sha256_mismatch", "url": source.url},
        )


def acquire_base(
    source: RootfsSource,
    scratch: Path,
    *,
    releasever: str,
    arch: str,
    virt_builder: Callable[..., None],
    downloader: Downloader,
) -> None:
    """Materialize a catalog ``source`` into the ``scratch`` qcow2.

    Args:
        source: The catalog row's base source (template or sha256-pinned cloud image).
        scratch: Destination path the base image is written to.
        releasever: Distro release version (reserved for the virt-builder seam).
        arch: Target architecture (reserved for the virt-builder seam).
        virt_builder: Seam invoked as ``virt_builder(template=..., output=scratch)`` for a
            :class:`VirtBuilderSource`.
        downloader: Seam invoked as ``downloader(url, scratch)`` for a :class:`CloudImageSource`;
            ``urllib``/OS errors it raises map to a ``base_unreachable`` configuration error.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` with ``details["reason"]`` of
            ``base_unreachable`` (download failed) or ``base_sha256_mismatch`` (digest mismatch).
    """
    del releasever, arch
    if isinstance(source, VirtBuilderSource):
        virt_builder(template=source.template, output=scratch)
        return
    _acquire_cloud_image(source, scratch, downloader)
