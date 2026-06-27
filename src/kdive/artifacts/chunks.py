"""Chunked artifact verification helpers (ADR-0104)."""

from __future__ import annotations

from typing import Protocol

from kdive.artifacts.storage import HeadResult, chunk_key
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory


class HeadStore(Protocol):
    """The minimal object-store surface chunk HEAD-verification needs."""

    def head(self, key: str) -> HeadResult | None: ...


def _build_failure(message: str, **details: object) -> CategorizedError:
    return CategorizedError(
        message,
        category=ErrorCategory.BUILD_FAILURE,
        details=details or None,
    )


def verify_chunks(store: HeadStore, prefix: str, entry: ManifestEntry) -> None:
    """HEAD-verify each declared chunk's stored ``(size, sha256)`` before reassembly.

    For a chunked artifact the per-chunk SHA-256 pins are the integrity anchor (ADR-0104 §4):
    each chunk object's stored checksum and size must match the manifest before the chunks are
    reassembled into the final object.

    Raises:
        CategorizedError: a chunk was never uploaded
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or disagrees with its manifest entry
            (:attr:`ErrorCategory.BUILD_FAILURE`).
    """
    if entry.chunks is None:
        raise CategorizedError(
            "artifact is not declared as chunked",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": entry.name},
        )
    for part_number, chunk in enumerate(entry.chunks, start=1):
        key = chunk_key(prefix, entry.name, part_number)
        head = store.head(key)
        if head is None:
            raise CategorizedError(
                f"declared chunk {part_number} of {entry.name!r} was never uploaded",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": entry.name, "part_number": part_number},
            )
        if head.size_bytes != chunk.size_bytes or head.checksum_sha256 != chunk.sha256:
            raise _build_failure(
                "uploaded chunk disagrees with its manifest",
                name=entry.name,
                part_number=part_number,
            )
