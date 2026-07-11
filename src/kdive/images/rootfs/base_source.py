"""Base-image acquirer for the local rootfs catalog (ADR-0251).

A catalog row's :class:`~kdive.images.rootfs.catalog.RootfsSource` is materialized into a
``scratch`` qcow2 by either invoking ``virt-builder`` against a template or downloading a
sha256-pinned cloud image and verifying its digest. Both paths fail closed: a digest mismatch or
an unreachable URL raises a ``CONFIGURATION_ERROR`` rather than handing a corrupt base downstream.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.client import HTTPMessage
from pathlib import Path
from typing import IO

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs.catalog import CloudImageSource, RootfsSource, VirtBuilderSource

type Downloader = Callable[[str, Path], None]

_DIGEST_CHUNK_BYTES = 1 << 20
_FETCH_SCHEMES = frozenset({"http", "https"})


class _CloudImageRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate the post-redirect URL's scheme so a 3xx cannot escape http/https.

    urllib's default redirect handler permits ``ftp://`` targets, so an allowlisted ``https``
    server could 302-redirect into ``ftp://internal-host``, escaping the http/https-only intent.
    Re-running the same allowlist on the redirect target keeps that intent across redirects.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_cloud_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _real_download(url: str, dest: Path) -> None:  # pragma: no cover - network IO
    """Stream ``url`` to ``dest`` using ``urllib`` (the default, un-unit-tested downloader)."""
    opener = urllib.request.build_opener(_CloudImageRedirect())
    with opener.open(url) as response, dest.open("wb") as handle:  # noqa: S310  # nosec B310
        while chunk := response.read(_DIGEST_CHUNK_BYTES):
            handle.write(chunk)


def _sha256_of(path: Path) -> str:
    """Compute the sha256 of ``path`` with a streaming read."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cloud_image_url(url: str) -> None:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme in _FETCH_SCHEMES:
        return
    raise CategorizedError(
        "cloud base image URL must use http or https",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "unsupported_url_scheme", "url": url, "scheme": scheme or "<missing>"},
    )


def _acquire_cloud_image(source: CloudImageSource, scratch: Path, downloader: Downloader) -> None:
    """Download ``source`` to ``scratch`` and verify its pinned sha256, failing closed."""
    _validate_cloud_image_url(source.url)
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
