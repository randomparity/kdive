"""Provider-neutral bounds for exact external-boot artifacts."""

from __future__ import annotations

from kdive.build_artifacts import validation as build_validation
from kdive.providers.ports.external_boot import (
    ArtifactSource,
    BundleSource,
    DebuginfoSource,
    ExternalBootPlan,
    InitrdSource,
)

MAX_MODULE_ENTRIES = 200_000
MAX_MODULE_REGULAR_BYTES = 8_589_934_592
MAX_MODULE_ARCHIVE_BYTES = MAX_MODULE_REGULAR_BYTES + MAX_MODULE_ENTRIES * 4096
# Maximum of the per-plan reservation below, before provider metadata (ADR-0747).
MAX_MATERIALIZATION_RESERVATION_BYTES = (
    build_validation._EXTERNAL_BOOT_DECODED_KERNEL_MAX_BYTES  # noqa: SLF001
    + build_validation._EXTERNAL_BOOT_INITRD_MAX_BYTES  # noqa: SLF001
    + build_validation._EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES  # noqa: SLF001
    + MAX_MODULE_REGULAR_BYTES
    + MAX_MODULE_ENTRIES * 1024
    + MAX_MODULE_ARCHIVE_BYTES * 2
    + build_validation._EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES  # noqa: SLF001
)


def source_byte_limit(source: ArtifactSource) -> int:
    """Return the bounded exact-object size allowed by the closed artifact source."""
    if isinstance(source, InitrdSource | DebuginfoSource):
        return source.size_bytes
    if isinstance(source, BundleSource):
        return build_validation._EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES  # noqa: SLF001
    raise TypeError("unsupported external-boot artifact source")


def materialization_reservation_bytes(plan: ExternalBootPlan) -> int:
    """Return the artifact bytes one activation may materialize (ADR-0602, #3129).

    Each authority adds its own fixed metadata overhead before comparing with its capacity.
    """
    initrd = 0 if plan.initrd is None else plan.initrd.size_bytes
    debuginfo = 0 if plan.debuginfo is None else plan.debuginfo.size_bytes
    return (
        plan.bundle.decoded_kernel_size_bytes
        + initrd
        + debuginfo
        + plan.module_obligation.uncompressed_bytes
        + plan.module_obligation.member_count * 1024
        + MAX_MODULE_ARCHIVE_BYTES * 2
        + source_byte_limit(plan.bundle)
    )
