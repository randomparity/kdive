"""Transport-encoding model + streaming strip-decode utility (ADR-0437, #1509)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import os

import pytest

from kdive.artifacts.transport_encoding import (
    _RANGE_CHUNK_BYTES,
    GZIP_ENCODING,
    IDENTITY_ENCODING,
    KNOWN_ENCODINGS,
    StripDecodeRequest,
    normalize_encoding,
    strip_gzip_to_writer,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


class _FakeRangedStore:
    """In-memory ranged-read store; records every ``(start, length)`` it is asked for."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.reads: list[tuple[int, int]] = []

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        del key
        self.reads.append((start, length))
        return self._data[start : start + length]


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _req(
    compressed: bytes, uncompressed_size: int, *, sha256: str | None = None
) -> StripDecodeRequest:
    return StripDecodeRequest(
        key="k",
        compressed_size=len(compressed),
        expected_sha256=_b64_sha256(compressed) if sha256 is None else sha256,
        uncompressed_size=uncompressed_size,
    )


# --- The model vocabulary ----------------------------------------------------------------


def test_known_encodings() -> None:
    assert frozenset({"gzip", "identity"}) == KNOWN_ENCODINGS
    assert GZIP_ENCODING == "gzip"
    assert IDENTITY_ENCODING == "identity"


def test_normalize_encoding_collapses_identity() -> None:
    assert normalize_encoding(None) is None
    assert normalize_encoding(IDENTITY_ENCODING) is None
    assert normalize_encoding(GZIP_ENCODING) == "gzip"
    # An unknown codec is returned unchanged for the caller to reject.
    assert normalize_encoding("zstd") == "zstd"


# --- The streaming strip-decode utility --------------------------------------------------


def test_strip_gzip_recovers_canonical_object() -> None:
    payload = b"canonical qcow2 bytes" * 100
    compressed = gzip.compress(payload)
    store = _FakeRangedStore(compressed)
    writer = io.BytesIO()

    result = strip_gzip_to_writer(store, _req(compressed, len(payload)), writer)

    assert writer.getvalue() == payload
    assert result.uncompressed_bytes == len(payload)


def test_strip_gzip_streams_ranged_reads_without_buffering_whole_object() -> None:
    # Incompressible payload > one range window forces multiple ranged reads and proves no
    # single read pulls the whole object into memory.
    payload = os.urandom(5 * 1024 * 1024)
    compressed = gzip.compress(payload)
    assert len(compressed) > _RANGE_CHUNK_BYTES  # otherwise the streaming claim is untested
    store = _FakeRangedStore(compressed)
    writer = io.BytesIO()

    result = strip_gzip_to_writer(store, _req(compressed, len(payload)), writer)

    assert writer.getvalue() == payload
    assert result.uncompressed_bytes == len(payload)
    assert len(store.reads) > 1  # streamed, not one whole-object read
    assert all(length <= _RANGE_CHUNK_BYTES for _, length in store.reads)


def test_strip_gzip_rejects_bomb_exceeding_bound() -> None:
    # A tiny gzip of a large canonical object whose declared bound is far smaller: the guard
    # fails closed the instant output exceeds the bound rather than expanding the whole thing.
    payload = b"\x00" * (4 * 1024 * 1024)
    compressed = gzip.compress(payload)
    bound = 4096
    store = _FakeRangedStore(compressed)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(compressed, bound), writer)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "exceeds the declared uncompressed_size bound" in str(exc.value)
    assert len(writer.getvalue()) <= bound + 1  # never expanded past the bound


def test_strip_gzip_rejects_transport_hash_mismatch() -> None:
    payload = b"canonical bytes"
    compressed = gzip.compress(payload)
    store = _FakeRangedStore(compressed)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(
            store, _req(compressed, len(payload), sha256="not-the-real-hash"), writer
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "transport checksum mismatch" in str(exc.value)


def test_strip_gzip_rejects_truncated_stream() -> None:
    payload = b"canonical bytes" * 50
    compressed = gzip.compress(payload)
    truncated = compressed[:-8]  # drop the gzip CRC/ISIZE trailer
    store = _FakeRangedStore(truncated)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(truncated, len(payload)), writer)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "truncated" in str(exc.value)


def test_strip_gzip_rejects_trailing_data_after_stream() -> None:
    # We strip a single gzip member; a concatenated/multi-member object fails closed with a clear
    # message rather than the confusing checksum-mismatch branch.
    payload = b"canonical bytes" * 20
    concatenated = gzip.compress(payload) + gzip.compress(b"trailing member")
    store = _FakeRangedStore(concatenated)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(concatenated, len(payload)), writer)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "trailing data after the gzip stream" in str(exc.value)


def test_strip_gzip_rejects_corrupt_stream() -> None:
    payload = b"canonical bytes" * 50
    corrupt = bytearray(gzip.compress(payload))
    corrupt[15] ^= 0xFF  # flip a byte inside the deflate body
    store = _FakeRangedStore(bytes(corrupt))
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(bytes(corrupt), len(payload)), writer)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
