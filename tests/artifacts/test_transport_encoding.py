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
    # Non-empty as well as capped: `<=` alone is satisfied by writing nothing, so a regression
    # in which `_drain` stopped writing (or was never reached) would leave this green.
    assert 0 < len(writer.getvalue()) <= bound + 1  # decompressed, and never past the bound


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


def _two_members() -> tuple[bytes, bytes, int]:
    """A boundary-aligned concatenated gzip: ``(whole, first_member, max_read)``.

    ``max_read`` is the first member's own length, **derived** so the pass consumes exactly that
    member in one range. ``eof`` then lands on a range boundary, which leaves ``unused_data``
    *empty* and the whole second member in ranges the pass never reads. That is the case
    ``_framing_defect``'s ``offset < compressed_size`` clause exists for, and the only case where
    it is the sole guard — a boundary that falls mid-range sets ``unused_data`` and the other
    clause carries it. Derived rather than hardcoded because the member's length is a property of
    the linked compressor, not of this test.
    """
    first = gzip.compress(b"canonical bytes" * 20)
    return first + gzip.compress(b"trailing member"), first, len(first)


def test_strip_gzip_trailing_data_with_damaged_bytes_reports_the_transport_gate() -> None:
    # The trailing-data branch reached with the whole second member unread: the first member's
    # ``eof`` stops the pass on a range boundary, and the unread ranges are hashed before the
    # verdict. Damaged here, so the digest overrules the multi-member defect.
    concatenated, _, max_read = _two_members()
    stored, request = _damaged(concatenated, len(concatenated) - 4, 0xFF)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(_FakeRangedStore(stored, max_read=max_read), request, io.BytesIO())

    _assert_transport_verdict(exc.value)


def test_strip_gzip_boundary_aligned_trailing_member_is_still_rejected() -> None:
    # Two guards in one, and both bite. (a) The framing clause: hashing the unread tail retired the
    # digest as an INDEPENDENT detector of trailing data -- on the old order the tail went unhashed,
    # so a boundary-aligned second member was caught by the checksum gate even if the clause were
    # gone. It no longer is, so ``offset < compressed_size`` is now the sole guard between a
    # multi-member object and a SILENT success that stages only the first member as a durable
    # rootfs base, past the qcow2-magic gate the first member's prefix satisfies. Delete that
    # clause and this test is what reddens. (b) The drain: a genuinely multi-member object the
    # agent really did upload only keeps CONFIGURATION_ERROR if the ranges the pass never read were
    # fed to the hasher anyway -- skip the drain and the digest comes up short and this reddens as
    # a transport mismatch instead.
    concatenated, first, max_read = _two_members()
    store = _FakeRangedStore(concatenated, max_read=max_read)
    payload_size = len(gzip.decompress(first))

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(store, _req(concatenated, payload_size), writer=io.BytesIO())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "trailing data after the gzip stream" in str(exc.value)
    # The pass stopped at the member boundary with `unused_data` empty, so the clause under test is
    # the one that fired -- without this the test could pass on the `unused_data` clause instead.
    assert store.reads[0] == (0, len(first))
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


@pytest.mark.parametrize("loop", ["decode-pass", "drain"], ids=["decode-pass", "drain"])
def test_strip_gzip_empty_range_inside_the_object_is_the_store_s_failure(loop: str) -> None:
    # A store that answers a range below the size its own HEAD reported with nothing has failed to
    # serve, and that is a claim about the READ, not about the object. Reported as a retryable
    # infrastructure failure from BOTH loops, so the same signal cannot mean two things depending
    # on which one saw it. Left as a bare `break` it reached the truncated branch, and once the
    # drain re-read the tail successfully the digest AGREED -- so the object was proven intact and
    # still told to re-upload, which inverts the very guarantee ADR-0523 §1 makes.
    payload = b"canonical bytes" * 400
    stored = gzip.compress(payload)
    served: list[int] = []
    # In `decode-pass` the empty range lands while the pass is still decoding; in `drain` a defect
    # has already stopped the pass, so the empty range lands in `_hash_remaining` instead.
    if loop == "drain":
        stored = bytes(bytearray(stored[:2]) + b"\xff" + stored[3:])  # bad method byte
    assert len(stored) > 32

    class _EmptyOnceStore(_FakeRangedStore):
        def get_range(self, key: str, *, start: int, length: int) -> bytes:
            if start == 16:
                served.append(start)
                return b""
            return super().get_range(key, start=start, length=length)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(
            _EmptyOnceStore(stored, max_read=16), _req(stored, len(payload)), io.BytesIO()
        )

    assert served == [16]  # the empty range was actually reached, in the loop under test
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "returned no bytes for a range" in str(exc.value)
    assert "truncated" not in str(exc.value)  # never the object-defect claim
    assert exc.value.details == {}  # no gate marker: this is not stored-object damage


def test_strip_gzip_store_fault_during_the_drain_keeps_the_decode_diagnosis() -> None:
    # The window the hash-only drain opens. ``get_range`` is called uncaught, and a connection
    # reset on one of these GETs is the likeliest failure on this path -- so a defect found
    # mid-pass, which lives only in a local, would die with the frame. The store's own retryable
    # verdict is the honest one (the digest is now unknowable, so nothing can be asserted about
    # the object), but losing WHY the decode failed is pure information loss: ADR-0523 §4's
    # terminal guarantee is conditional on the drain completing, and this is what records it.
    #
    # A NOTE rather than ``raise ... from``: the store chained its own botocore cause onto this
    # error already, and taking that over would render the causality backwards -- the traceback
    # would read as though the gzip corruption caused the connection reset.
    payload = b"canonical bytes" * 400
    corrupt = bytearray(gzip.compress(payload))
    corrupt[2] = 0xFF  # the gzip method byte: zlib rejects the FIRST range, deterministically
    stored = bytes(corrupt)
    assert len(stored) > 16  # otherwise the pass consumes the object and no drain read happens
    root = ConnectionResetError("peer went away")
    fault = CategorizedError("get_range on 'k' failed", category=ErrorCategory.TRANSPORT_FAILURE)
    fault.__cause__ = (
        root  # as `ObjectStore.get_range` raises it: `_infrastructure_error(...) from err`
    )

    class _FaultingStore(_FakeRangedStore):
        def get_range(self, key: str, *, start: int, length: int) -> bytes:
            if start > 0:  # the pass stopped inside range 0, so every later read is the drain's
                raise fault
            return super().get_range(key, start=start, length=length)

    with pytest.raises(CategorizedError) as exc:
        strip_gzip_to_writer(_FaultingStore(stored, max_read=16), _req(stored, 6000), io.BytesIO())

    assert exc.value is fault  # passed through, not reclassified as an object defect
    assert exc.value.__cause__ is root  # the store's real root cause is NOT taken over
    assert any("gzip transport stream is corrupt" in note for note in exc.value.__notes__)


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
    # Non-empty as well as capped: `<=` alone is satisfied by writing nothing, so a regression
    # in which `_drain` stopped writing (or was never reached) would leave this green.
    assert 0 < len(writer.getvalue()) <= bound + 1  # decompressed, and never past the bound
    assert sum(length for _, length in store.reads) == len(stored)
    starts = [start for start, _ in store.reads]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)  # no range re-read
