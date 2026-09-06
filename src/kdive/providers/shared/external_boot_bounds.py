"""Provider-neutral bounds for exact external-boot artifacts."""

from __future__ import annotations

from kdive.build_artifacts import validation as build_validation
from kdive.providers.ports.external_boot import ArtifactSource, BundleSource, InitrdSource

MAX_MODULE_ENTRIES = 200_000
MAX_MODULE_REGULAR_BYTES = 8_589_934_592
MAX_MODULE_ARCHIVE_BYTES = MAX_MODULE_REGULAR_BYTES + MAX_MODULE_ENTRIES * 4096


def source_byte_limit(source: ArtifactSource) -> int:
    """Return the bounded exact-object size allowed by the closed artifact source."""
    if isinstance(source, InitrdSource):
        return source.size_bytes
    if isinstance(source, BundleSource):
        return build_validation._EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES  # noqa: SLF001
    raise TypeError("unsupported external-boot artifact source")
