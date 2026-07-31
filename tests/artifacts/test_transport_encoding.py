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
    TRANSPORT_CHECKSUM_GATE,
    StripDecodeRequest,
    normalize_encoding,
    strip_gzip_to_writer,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


class _FakeRangedStore:
    """In-memory ranged-read store; records every ``(start, length)`` it is asked for.

    ``max_read`` caps how much any one range returns, which is how the multi-range paths are
    exercised without a >4 MiB fixture: a real store may return fewer bytes than asked, and the
    decode walks ``compressed_size`` in ``_RANGE_CHUNK_BYTES`` windows either way.
    """

    def __init__(self, data: bytes, *, max_read: int | None = None) -> None:
        self._data = data
        self._max_read = max_read
        self.reads: list[tuple[int, int]] = []

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        del key
        if self._max_read is not None:
            length = min(length, self._max_read)
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


# The object-defect side of the ADR-0445 split: each of these is a defect in the object the agent
# uploaded, so retrying the same key re-reads the same defect and the category stays terminal.
# Widening the retryable constructor over any of them reddens here.


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


def test_strip_gzip_transport_hash_mismatch_is_retryable_infrastructure_failure() -> None:
    # #1523 / ADR-0445. The bytes read back do not hash to the checksum the signed PUT bound.
    # That single observation covers two modes — transient GET-side transport corruption and
    # permanent post-PUT bit rot — so it is reported RETRYABLE, matching the identity staging
    # path (ADR-0434 §2) and the catalog digest check. It is deliberately NOT the category the
    # object-defect branches below use: this asserts the split, not just one branch.
    payload = b"canonical bytes"
    compressed = gzip.compress(payload)
    store = _FakeRangedStore(compressed)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(
            store, _req(compressed, len(payload), sha256="not-the-real-hash"), writer
        )

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "transport checksum mismatch" in str(exc.value)
    # The remediation the identity path also gives, and not the old "do not retry" advice that
    # contradicted a retryable category.
    assert "retry, and if it persists the stored object is damaged" in str(exc.value)
    assert "do not retry" not in str(exc.value)


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


# The transport gate rules first (#1548 / ADR-0523). Each object-defect branch above asserts "the
# object the agent uploaded is defective"; that claim is only true when the bytes read back are the
# bytes the signed PUT bound. Every case below is the SAME damage as one of the four branches, with
# the checksum of the *undamaged* object declared — so the stored bytes rotted post-PUT and the
# verdict must be the retryable one the identity path gives for byte-identical damage.


def _damaged(compressed: bytes, index: int, mask: int) -> tuple[bytes, StripDecodeRequest]:
    """Stored bytes with one byte flipped, declared under the PRISTINE object's checksum."""
    stored = bytearray(compressed)
    stored[index] ^= mask
    return bytes(stored), StripDecodeRequest(
        key="k",
        compressed_size=len(stored),
        expected_sha256=_b64_sha256(compressed),
        uncompressed_size=len(gzip.decompress(compressed)),
    )


def _assert_transport_verdict(exc: CategorizedError) -> None:
    assert exc.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.details["gate"] == TRANSPORT_CHECKSUM_GATE
    assert "transport checksum mismatch" in str(exc)


def test_strip_gzip_every_single_byte_flip_reports_the_transport_gate() -> None:
    # The acceptance criterion, exhaustively rather than by sampling. ADR-0445 §6 measured a
    # single-bit sweep of the deflate body landing on three different branches -- 225 corrupt-stream
    # / 13 bomb bound / 10 checksum gate of 248 flips -- with the codec, not the damage, deciding
    # whether the agent was told to retry. Sweeping EVERY byte (header, deflate body and CRC/ISIZE
    # trailer) under every single-bit mask plus 0xFF, the whole table must now collapse onto one
    # verdict. A sweep rather than a fixed offset because which branch a flip reaches is a property
    # of the linked zlib build's exact deflate encoding, not of this test.
    payload = b"canonical qcow2 bytes" * 8
    compressed = gzip.compress(payload)
    masks = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0xFF)
    swept = 0

    for index in range(len(compressed)):
        for mask in masks:
            stored, request = _damaged(compressed, index, mask)
            with pytest.raises(CategorizedError) as exc:
                strip_gzip_to_writer(_FakeRangedStore(stored), request, io.BytesIO())
            _assert_transport_verdict(exc.value)
            swept += 1

    assert swept == len(compressed) * len(masks) > 0  # the sweep ran, rather than vacuously passing


def test_strip_gzip_truncated_stored_object_reports_the_transport_gate() -> None:
    # The truncated branch: a post-PUT truncation, so the hasher has already absorbed every stored
    # byte and the digest is simply short of the signed one.
    payload = b"canonical bytes" * 50
    compressed = gzip.compress(payload)
    truncated = compressed[:-8]  # the gzip CRC/ISIZE trailer is gone
    request = StripDecodeRequest(
        key="k",
        compressed_size=len(truncated),
        expected_sha256=_b64_sha256(compressed),  # the checksum of the WHOLE object
        uncompressed_size=len(payload),
    )

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(_FakeRangedStore(truncated), request, io.BytesIO())

    _assert_transport_verdict(exc.value)


def test_strip_gzip_trailing_data_with_damaged_bytes_reports_the_transport_gate() -> None:
    # The trailing-data branch reached with an unread tail: ``max_read`` forces many ranges, the
    # first member's ``eof`` stops the pass early, and the unread ranges are hashed before the
    # verdict. Damaged here, so the digest overrules the multi-member defect.
    payload = b"canonical bytes" * 20
    concatenated = gzip.compress(payload) + gzip.compress(b"trailing member")
    stored, request = _damaged(concatenated, len(concatenated) - 4, 0xFF)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(_FakeRangedStore(stored, max_read=16), request, io.BytesIO())

    _assert_transport_verdict(exc.value)


def test_strip_gzip_trailing_data_in_unread_ranges_still_hashes_them() -> None:
    # The converse, and the assertion that bites: a genuinely multi-member object the agent really
    # did upload keeps CONFIGURATION_ERROR. That only holds if the ranges the pass never read --
    # everything past the first member's ``eof`` -- were fed to the hasher anyway. Skip that drain
    # and the digest comes up short, and this reddens as a transport mismatch.
    payload = b"canonical bytes" * 20
    concatenated = gzip.compress(payload) + gzip.compress(b"trailing member")
    store = _FakeRangedStore(concatenated, max_read=16)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(concatenated, len(payload)), writer=io.BytesIO())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "trailing data after the gzip stream" in str(exc.value)
    assert sum(length for _, length in store.reads) == len(concatenated)  # the tail was read


@pytest.mark.parametrize(
    ("payload", "bound", "expected"),
    [
        # A corrupt deflate stream and a bomb-bound trip: the two defects ``_drain`` finds mid-pass,
        # where the frame that finds them can see neither the store nor the hasher. Each keeps its
        # terminal category when the stored bytes ARE the bytes signed at PUT. The corrupt case
        # declares its true canonical size; the bomb case declares a bound the object blows past.
        (b"canonical bytes" * 400, 6000, "gzip transport stream is corrupt"),
        (b"\x00" * (1024 * 1024), 4096, "exceeds the declared uncompressed_size bound"),
    ],
    ids=["deflate-corrupt", "bomb-bound"],
)
def test_strip_gzip_mid_pass_defect_keeps_configuration_error_when_the_digest_agrees(
    payload: bytes, bound: int, expected: str
) -> None:
    # The other half of the split, for the two branches that raise from inside ``_drain``. The
    # defect stops the pass with most of the object unread, so this passes only if the remaining
    # ranges were drained hash-only: drop that drain and the digest is short and this reddens.
    compressed = bytearray(gzip.compress(payload))
    if expected.startswith("gzip"):
        compressed[15] ^= 0xFF  # damage the agent uploaded, and signed
    stored = bytes(compressed)
    store = _FakeRangedStore(stored, max_read=16)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(stored, bound), io.BytesIO())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert expected in str(exc.value)
    assert exc.value.details == {}  # no gate marker: this is not the checksum gate


def test_strip_gzip_hash_only_drain_never_expands_a_bomb_and_reads_each_byte_once() -> None:
    # The bound holds across the new drain. A bomb whose stored bytes also rotted takes the
    # transport verdict, and getting there must not reopen decompression: the writer still stops at
    # the bound, and the two loops between them tile the object exactly once -- the drain is
    # bounded by ``compressed_size``, not a second full pass.
    payload = b"\x00" * (8 * 1024 * 1024)
    compressed = gzip.compress(payload)
    bound = 4096
    stored = bytearray(compressed)
    stored[-1] ^= 0xFF  # ISIZE rot, so the signed checksum no longer matches
    request = StripDecodeRequest(
        key="k",
        compressed_size=len(stored),
        expected_sha256=_b64_sha256(compressed),
        uncompressed_size=bound,
    )
    store = _FakeRangedStore(bytes(stored), max_read=64)
    writer = io.BytesIO()

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, request, writer)

    _assert_transport_verdict(exc.value)
    assert len(writer.getvalue()) <= bound + 1  # never expanded past the bound
    assert sum(length for _, length in store.reads) == len(stored)
    starts = [start for start, _ in store.reads]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)  # no range re-read
