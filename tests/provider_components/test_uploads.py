"""Chunk-aware upload value types (ADR-0104)."""

from __future__ import annotations

from kdive.provider_components.uploads import (
    MAX_PART_BYTES,
    MIN_PART_BYTES,
    SINGLE_PUT_MAX_BYTES,
    ChunkEntry,
    ManifestEntry,
)


def test_manifest_entry_defaults_chunks_to_none() -> None:
    entry = ManifestEntry(name="vmlinux", sha256="abc", size_bytes=10)
    assert entry.chunks is None


def test_manifest_entry_carries_ordered_chunks() -> None:
    chunks = (ChunkEntry(sha256="c0", size_bytes=5), ChunkEntry(sha256="c1", size_bytes=5))
    entry = ManifestEntry(name="vmlinux", sha256="whole", size_bytes=10, chunks=chunks)
    assert entry.chunks == chunks
    assert entry.chunks is not None
    assert entry.chunks[0].size_bytes == 5


def test_part_size_constants_are_ordered() -> None:
    assert MIN_PART_BYTES < MAX_PART_BYTES == SINGLE_PUT_MAX_BYTES
