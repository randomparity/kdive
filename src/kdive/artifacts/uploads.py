"""Shared upload declaration value types."""

from __future__ import annotations

from typing import NamedTuple

# Real S3 caps a single PUT at 5 GiB; a chunk reassembled via UploadPartCopy must be a
# valid multipart part: <= 5 GiB, and >= 5 MiB unless it is the final part (ADR-0104 §5).
SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024
MAX_PART_BYTES = 5 * 1024 * 1024 * 1024
MIN_PART_BYTES = 5 * 1024 * 1024
MAX_PARTS = 10_000


class ChunkEntry(NamedTuple):
    """One declared chunk of a chunked artifact: its base64 SHA-256 and byte size."""

    sha256: str
    size_bytes: int


class ManifestEntry(NamedTuple):
    """One declared artifact: name, base64 SHA-256, byte size, optional chunks, optional encoding.

    ``chunks is None`` is a single-PUT artifact. When ``chunks`` is set the artifact is
    uploaded in pieces and reassembled at finalize: ``sha256`` is then the advisory
    whole-object hash and ``size_bytes`` is the whole-object total (equal to the chunk-size
    sum), while integrity is anchored on each :class:`ChunkEntry`'s ``sha256`` (ADR-0104 §2).

    ``encoding`` is an optional *transport wrapper* on the stored object (ADR-0437): ``None``
    (or the declared ``"identity"``, normalized to ``None``) means the stored bytes already are
    the canonical object; ``"gzip"`` means kdive strips the encoding on download to recover the
    canonical object, whose size is ``uncompressed_size`` (required with a non-identity
    ``encoding``). ``sha256``/``size_bytes`` always describe the stored (transport) bytes, so the
    transport checksum stays consistent with the signed PUT. An encoded upload is single-PUT only
    (``encoding`` with ``chunks`` is rejected at declaration).
    """

    name: str
    sha256: str
    size_bytes: int
    chunks: tuple[ChunkEntry, ...] | None = None
    encoding: str | None = None
    uncompressed_size: int | None = None
