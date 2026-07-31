"""Transport-encoding model and the shared streaming strip-decode utility (ADR-0437).

``encoding`` is a *transport wrapper* on an agent upload, semantically distinct from the payload
format: the stored object may be a gzip stream whose gunzip yields the *canonical object*, on which
the existing per-artifact format validation runs. This module owns the codec vocabulary and the
consumer-agnostic decode utility; the declaration validator imports the codec constants and a future
consumer (rootfs, #1510) imports :func:`strip_gzip_to_writer`. No consumer is wired here.
"""

from __future__ import annotations

import base64
import hashlib
import zlib
from dataclasses import dataclass
from typing import IO, NamedTuple, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory

GZIP_ENCODING = "gzip"
IDENTITY_ENCODING = "identity"
# The recognized transport codecs. ``identity`` is the explicit spelling of "no encoding" (the
# stored bytes already are the canonical object); ``gzip`` is the only non-identity codec in the
# first cut. ADR-0437 leaves room for ``zstd``/``xz`` here later with no schema change.
KNOWN_ENCODINGS = frozenset({GZIP_ENCODING, IDENTITY_ENCODING})

# Per-GET ranged-read window and the per-decompress output cap, so neither the compressed read nor
# the decompressed write ever buffers the multi-GiB canonical object in memory (mirrors the bounded
# gunzip in ``build_artifacts/validation.py``).
_RANGE_CHUNK_BYTES = 4 * 1024 * 1024


def normalize_encoding(encoding: str | None) -> str | None:
    """Return the effective codec, collapsing absent/``identity`` to ``None`` (identity).

    Args:
        encoding: The declared ``encoding`` value, or ``None`` when absent.

    Returns:
        ``None`` when the declaration is identity (absent or the explicit ``"identity"``), else the
        codec name unchanged (an unknown codec is returned as-is for the validator to reject).
    """
    if encoding is None or encoding == IDENTITY_ENCODING:
        return None
    return encoding


class RangedReadStore(Protocol):
    """The narrow store seam the decode utility needs: sequential ranged reads of one key."""

    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


@dataclass(frozen=True)
class StripDecodeRequest:
    """Inputs for one streaming strip-decode of a gzip-encoded transport object.

    Attributes:
        key: The store key of the compressed (transport) object.
        compressed_size: The stored object's total byte size — the range the reads walk.
        expected_sha256: Base64 SHA-256 of the *compressed* bytes (the transport checksum the signed
            PUT bound; ADR-0437). Verified at end-of-stream.
        uncompressed_size: The declared canonical-object size in bytes — the hard upper bound on
            decompressed output (the gzip-bomb guard).
    """

    key: str
    compressed_size: int
    expected_sha256: str
    uncompressed_size: int


class StripDecodeResult(NamedTuple):
    """The outcome of a successful strip-decode: how many canonical bytes were written."""

    uncompressed_bytes: int


# The ``details["gate"]`` marker on a transport-checksum rejection. A consumer cannot identify that
# gate from the category alone: the store's own ``get_range`` faults (a connection reset on any of
# the hundreds of ranged GETs a multi-GiB stage issues) propagate through this module as
# ``INFRASTRUCTURE_FAILURE`` too, so a category test would mistake a transport blip for
# stored-object damage. Exported because the uploaded-rootfs fetch logs on this gate (ADR-0445 §4).
TRANSPORT_CHECKSUM_GATE = "transport_checksum"


def _object_error(detail: str) -> CategorizedError:
    """A defect in the object the agent uploaded: retrying the same key re-reads the same defect.

    Only reached once the transport digest has agreed that the stored bytes are the bytes signed at
    PUT, which is what makes the claim true rather than merely nearest (ADR-0523).
    """
    return CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)


def _transport_error(detail: str) -> CategorizedError:
    """The bytes read back are not the bytes the signed PUT bound: retryable (ADR-0445).

    The two constructors exist so the category follows from *what is being asserted* rather than
    from which helper is nearest. A single ``_decode_error`` covered all three of this module's
    failure outcomes, so the checksum mismatch inherited the object-defect category by proximity
    and disagreed with the identity staging path over the byte-identical failure (#1523). One
    observation, two modes: transient GET-side transport corruption, which a bare retry clears,
    and permanent post-PUT bit rot, which it does not.

    What the category controls is the **agent-visible** ``retryable`` boolean
    ``mcp/responses.py`` derives from it, and the remediation the message gives — not any
    automatic re-attempt. A staging failure reaches the queue through the provision handler, which
    sets ``terminal`` on the error before re-raising (``jobs/handlers/systems.py``), so the job
    dead-letters on the first attempt under *either* category and nothing is re-downloaded. The
    retry this advises is the agent's own, against a System that is already terminally failed.

    **Reach (ADR-0523, closing ADR-0445 §6).** The digest is now consulted *before* any
    object-defect branch raises, so it reaches every shape of stored-byte damage: a flipped bit in
    the deflate body or the CRC/ISIZE trailer, a post-PUT truncation, and damage that leaves the
    decoded stream intact (header fields, deflate padding bits, a wholesale replacement by another
    well-formed gzip under the bound) all land here. Under ADR-0445 §6 zlib's framing tripped first
    for most of them and they reported the object-defect category instead, which disagreed with the
    identity path over byte-identical damage.
    """
    return CategorizedError(
        detail,
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"gate": TRANSPORT_CHECKSUM_GATE},
    )


class _ObjectDefect(Exception):
    """A gzip framing or bound defect, carried to the seam that can decide what it means.

    Never raised out of this module. The claim an object-defect error makes — *the object the agent
    uploaded is defective* — is unfounded whenever the bytes read back are not the bytes the signed
    PUT bound, and the code that finds a defect cannot tell: :func:`_drain` sees a decompressor and
    an input range, not the store, the key, or the running hash. So a defect is reported upward as
    a value, and :func:`strip_gzip_to_writer` — which holds all three — compares the transport
    digest first and picks the category from that (ADR-0523, closing ADR-0445 §6 / #1548).
    """


class _DecodePass(NamedTuple):
    """How far one decode pass got: output written, stored bytes consumed, and any defect found.

    ``offset`` is where the transport hash still needs feeding from: a defect stops the pass early,
    leaving the tail of the stored object unread and the digest incomplete.
    """

    written: int
    offset: int
    defect: _ObjectDefect | None


def strip_gzip_to_writer(
    store: RangedReadStore, request: StripDecodeRequest, writer: IO[bytes]
) -> StripDecodeResult:
    """Stream-gunzip a gzip transport object into ``writer``, bounded and hash-verified.

    Single pass over the compressed object via sequential ranged reads: each range is gunzipped into
    ``writer`` (never buffering the whole canonical object) while the *compressed* bytes are hashed.
    Decompressed output is capped at ``request.uncompressed_size`` — the instant it would exceed the
    bound the decode stops (gzip bomb), so a bomb is never expanded. At end-of-stream the gzip
    trailer must have been reached (``zlib`` verifies its CRC/ISIZE) and the compressed hash must
    match ``request.expected_sha256`` (transport verify). Every failure raises a
    ``CategorizedError`` with a self-correcting message, under one of the two categories ADR-0445
    split apart: a bomb or a corrupt/truncated/multi-member stream is a defect in the uploaded
    object (``CONFIGURATION_ERROR``, terminal), while a hash mismatch says the bytes read back are
    not the bytes signed at PUT (``INFRASTRUCTURE_FAILURE``, retryable — the same category the
    identity staging path and the catalog digest check use for it).

    Note the gate *order*, which ADR-0523 inverted: the transport digest is compared **first**, and
    an object defect is only reported once the stored bytes have been proven to be the bytes signed
    at PUT. A defect found mid-stream therefore does not raise where it is found — the pass returns
    it, the unread ranges are drained **hash-only** (no decompress, no write, so the bound still
    holds and no bomb is expanded), and the digest decides. Under the old order the framing checks
    ran first and damaged stored bytes reported the terminal category on the gzip path where the
    identity path reported the retryable one (ADR-0445 §6). The caller owns atomic staging, so a
    raised error discards the partial output already written.

    A checksum rejection carries ``details["gate"] == TRANSPORT_CHECKSUM_GATE``; the object-defect
    rejections carry no ``details`` at all. A caller that has identifying context (the
    uploaded-rootfs fetch attaches its ``system_id``) annotates them on the way out.

    Note that ``store.get_range`` is called **uncaught** in both read loops, so the store's own
    faults propagate through unchanged — including ``INFRASTRUCTURE_FAILURE`` for a connection
    reset on any of the hundreds of ranged GETs a multi-GiB object takes. That is why the gate
    marker exists: a consumer that distinguished the checksum rejection by *category* would report
    every transport blip as stored-object damage.

    Args:
        store: A ranged-read store over the compressed object.
        request: The key, compressed size, expected compressed hash, and uncompressed-size bound.
        writer: A binary sink the canonical (decompressed) bytes stream into.

    Returns:
        A :class:`StripDecodeResult` with the number of decompressed bytes written.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` (with the ``TRANSPORT_CHECKSUM_GATE`` marker)
            on a transport-checksum mismatch; ``CONFIGURATION_ERROR`` on a gzip bomb or a corrupt,
            truncated, or multi-member gzip stream whose stored bytes *do* match that checksum. The
            store's own errors from ``get_range`` propagate unchanged, and carry no marker.
    """
    hasher = hashlib.sha256()
    decoded = _decode_pass(store, request, writer, hasher)
    try:
        _hash_remaining(store, request, hasher, start=decoded.offset)
    except Exception as fault:
        # The drain reads uncaught, so the store's own faults propagate — and this frame is the
        # only one that ever held ``decoded.defect``, which would otherwise die with it. A fault
        # here leaves the digest unknowable, so the store's retryable verdict is the honest one and
        # passes through unchanged; only the decode diagnosis is chained on, so a postmortem can
        # still see the object did not decode. This window is why ADR-0523 §4's terminal guarantee
        # is conditional on the drain completing, and the branch that widened it records that.
        if decoded.defect is None:
            raise
        raise fault from decoded.defect

    actual = base64.b64encode(hasher.digest()).decode("ascii")
    if actual != request.expected_sha256:
        # Chained, not merged. A defect found before the digest disagreed is a real observation
        # about *how* the stored bytes are broken, and an operator investigating bit rot wants it —
        # but it must not reach the agent, whose message ADR-0523 deliberately strips of the decode
        # advice (the bomb branch's "re-declare with the correct uncompressed_size" is wrong
        # precisely here). ``details`` is documented as surfaceable in responses, so the chain is
        # the carrier: the consumer's operator log lifts it off ``__cause__``.
        raise _transport_error(
            "transport checksum mismatch: the stored object's SHA-256 does not match the checksum "
            "signed at upload; retry, and if it persists the stored object is damaged and must be "
            "re-uploaded"
        ) from decoded.defect
    if decoded.defect is not None:
        raise _object_error(str(decoded.defect)) from decoded.defect
    return StripDecodeResult(uncompressed_bytes=decoded.written)


def _decode_pass(
    store: RangedReadStore,
    request: StripDecodeRequest,
    writer: IO[bytes],
    hasher: hashlib._Hash,
) -> _DecodePass:
    """Read the stored object in ranges, hashing every byte and gunzipping it into ``writer``.

    Returns at the first defect rather than raising, so the caller can finish the digest before
    deciding whether the defect is the agent's or the store's. A short read (an empty range) ends
    the pass too: the tail is then unread, which the digest catches as a mismatch.
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 + MAX_WBITS selects gzip framing
    written = 0
    offset = 0
    while offset < request.compressed_size and not decompressor.eof:
        length = min(_RANGE_CHUNK_BYTES, request.compressed_size - offset)
        chunk = store.get_range(request.key, start=offset, length=length)
        if not chunk:
            break
        offset += len(chunk)
        hasher.update(chunk)
        try:
            written = _drain(
                decompressor, chunk, writer, bound=request.uncompressed_size, written=written
            )
        except _ObjectDefect as defect:
            return _DecodePass(written=written, offset=offset, defect=defect)
    return _DecodePass(
        written=written,
        offset=offset,
        defect=_framing_defect(decompressor, offset=offset, request=request),
    )


def _framing_defect(
    decompressor: zlib._Decompress, *, offset: int, request: StripDecodeRequest
) -> _ObjectDefect | None:
    """The end-of-pass framing verdict: an incomplete member, a second one, or neither."""
    if not decompressor.eof:
        return _ObjectDefect(
            "gzip transport stream is truncated: it ended before the gzip trailer, so the "
            "canonical object is incomplete; re-upload the full object"
        )
    if decompressor.unused_data or offset < request.compressed_size:
        # The single gzip member ended before the stored object did: trailing bytes remain — either
        # after the member within a read range (``unused_data``) or in an unread range once ``eof``
        # stopped the pass early. That is a concatenated/multi-member gzip or garbage after the
        # stream, and we strip one gzip member only. The unread tail is still hashed before this
        # verdict is acted on, which is what lets the digest overrule it (ADR-0523).
        return _ObjectDefect(
            "trailing data after the gzip stream: the stored object is not a single gzip member "
            "(concatenated/multi-member gzip is not supported); re-upload a single gzip of the "
            "canonical object"
        )
    return None


def _hash_remaining(
    store: RangedReadStore, request: StripDecodeRequest, hasher: hashlib._Hash, *, start: int
) -> None:
    """Feed the stored bytes past ``start`` into ``hasher``, decompressing and writing nothing.

    The whole point is what it does *not* do. A defect stops the decode pass mid-object, so the
    digest the transport gate needs is incomplete; this completes it by re-reading only the ranges
    the pass never reached — bounded by ``compressed_size``, and touching neither the decompressor
    nor the writer, so the bomb bound the pass tripped is never reopened (ADR-0523). Every byte of
    the object is therefore read exactly once across the two loops. A no-op on the success path,
    where the pass already consumed the whole object.

    It does replace an early abort with a full read on the two branches that fail mid-pass, and on
    the bomb branch that is not free: a bomb has no successful stage to compare against, so a
    hostile object that trips the cap after a few KiB now costs a read of the whole stored object —
    up to the single-PUT ceiling — where it used to cost one range. ADR-0523's Consequences weigh
    that; bounding or skipping the drain on a size heuristic is not the answer, because it restores
    the codec-decides-the-verdict asymmetry the ADR exists to close.
    """
    offset = start
    while offset < request.compressed_size:
        length = min(_RANGE_CHUNK_BYTES, request.compressed_size - offset)
        chunk = store.get_range(request.key, start=offset, length=length)
        if not chunk:
            return
        offset += len(chunk)
        hasher.update(chunk)


def _drain(
    decompressor: zlib._Decompress,
    data: bytes,
    writer: IO[bytes],
    *,
    bound: int,
    written: int,
) -> int:
    """Gunzip one input range fully into ``writer``, capping total output at ``bound``.

    Re-feeds ``unconsumed_tail`` so the whole range is decompressed, but limits each decompress call
    to ``_RANGE_CHUNK_BYTES`` of output (memory bound) and the running total to ``bound + 1`` so the
    guard trips on the first byte past the declared canonical size (gzip-bomb guard).

    Raises :class:`_ObjectDefect` rather than a ``CategorizedError``: this frame cannot see the
    transport digest, so it reports *what* is wrong and leaves *whose fault it is* to the caller.
    """
    while data:
        max_len = min(_RANGE_CHUNK_BYTES, bound - written + 1)
        try:
            produced = decompressor.decompress(data, max_len)
        except zlib.error as exc:
            raise _ObjectDefect(
                "gzip transport stream is corrupt: decompression failed; re-upload the object"
            ) from exc
        if produced:
            writer.write(produced)
            written += len(produced)
            if written > bound:
                raise _ObjectDefect(
                    "decompressed output exceeds the declared uncompressed_size bound "
                    f"({bound} bytes): the object is not a valid gzip of that size (a gzip bomb "
                    "or a wrong uncompressed_size); re-declare with the correct uncompressed_size "
                    "or upload the correct object"
                )
        data = decompressor.unconsumed_tail
    return written
