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


def _object_error(detail: str) -> CategorizedError:
    """A defect in the object the agent uploaded: retrying the same key re-reads the same defect."""
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

    **Reach (ADR-0445 §6).** On this path the hash comparison is the *last* gate, and zlib's gzip
    framing catches most damage first: a flipped bit in the deflate body or the CRC/ISIZE trailer,
    or a post-PUT truncation, raises :func:`_object_error` before the digest is ever compared —
    body corruption lands on the corrupt-stream branch or, in a content-dependent minority where it
    desynchronises the Huffman decode, on the bomb bound, whose message then wrongly blames the
    declared ``uncompressed_size``. What reaches the digest is damage leaving the decoded stream
    and framing intact: header fields, deflate padding bits, or a wholesale replacement by another
    well-formed gzip under the bound. #1548 tracks closing the residual by consulting the digest
    before declaring an object defect.
    """
    return CategorizedError(detail, category=ErrorCategory.INFRASTRUCTURE_FAILURE)


def strip_gzip_to_writer(
    store: RangedReadStore, request: StripDecodeRequest, writer: IO[bytes]
) -> StripDecodeResult:
    """Stream-gunzip a gzip transport object into ``writer``, bounded and hash-verified.

    Single pass over the compressed object via sequential ranged reads: each range is gunzipped into
    ``writer`` (never buffering the whole canonical object) while the *compressed* bytes are hashed.
    Decompressed output is capped at ``request.uncompressed_size`` — the instant it would exceed the
    bound the call fails closed (gzip bomb), so a bomb is never expanded. At end-of-stream the gzip
    trailer must have been reached (``zlib`` verifies its CRC/ISIZE) and the compressed hash must
    match ``request.expected_sha256`` (transport verify). Every failure raises a
    ``CategorizedError`` with a self-correcting message, under one of the two categories ADR-0445
    split apart: a bomb or a corrupt/truncated/multi-member stream is a defect in the uploaded
    object (``CONFIGURATION_ERROR``, terminal), while a hash mismatch says the bytes read back are
    not the bytes signed at PUT (``INFRASTRUCTURE_FAILURE``, retryable — the same category the
    identity staging path and the catalog digest check use for it). Note the gate *order*: the hash
    is compared last, after the framing and bound checks, so damaged stored bytes that also break
    the gzip framing report the object-defect category (ADR-0445 §6, residual tracked in #1548).
    The caller owns atomic staging, so a raised error discards the partial output already written.

    The raised errors carry no ``details``; a caller that has identifying context (the
    uploaded-rootfs fetch attaches its ``system_id``) annotates them on the way out.

    Args:
        store: A ranged-read store over the compressed object.
        request: The key, compressed size, expected compressed hash, and uncompressed-size bound.
        writer: A binary sink the canonical (decompressed) bytes stream into.

    Returns:
        A :class:`StripDecodeResult` with the number of decompressed bytes written.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on a gzip bomb or a corrupt, truncated, or
            multi-member gzip stream; ``INFRASTRUCTURE_FAILURE`` on a transport-checksum mismatch.
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 + MAX_WBITS selects gzip framing
    hasher = hashlib.sha256()
    bound = request.uncompressed_size
    written = 0
    offset = 0
    while offset < request.compressed_size and not decompressor.eof:
        length = min(_RANGE_CHUNK_BYTES, request.compressed_size - offset)
        chunk = store.get_range(request.key, start=offset, length=length)
        if not chunk:
            break
        offset += len(chunk)
        hasher.update(chunk)
        written = _drain(decompressor, chunk, writer, bound=bound, written=written)
    if not decompressor.eof:
        raise _object_error(
            "gzip transport stream is truncated: it ended before the gzip trailer, so the "
            "canonical object is incomplete; re-upload the full object"
        )
    if decompressor.unused_data or offset < request.compressed_size:
        # The single gzip member ended before the stored object did: trailing bytes remain — either
        # after the member within a read range (``unused_data``) or in an unread range once ``eof``
        # stopped the loop early. That is a concatenated/multi-member gzip or garbage after the
        # stream; we strip one gzip member only, so fail closed with a clear message rather than the
        # checksum-mismatch branch below. Since ADR-0445 that ordering also decides the category:
        # this is an object defect (terminal), where falling through would report it retryable.
        raise _object_error(
            "trailing data after the gzip stream: the stored object is not a single gzip member "
            "(concatenated/multi-member gzip is not supported); re-upload a single gzip of the "
            "canonical object"
        )
    actual = base64.b64encode(hasher.digest()).decode("ascii")
    if actual != request.expected_sha256:
        raise _transport_error(
            "transport checksum mismatch: the stored object's SHA-256 does not match the checksum "
            "signed at upload; retry, and if it persists the stored object is damaged and must be "
            "re-uploaded"
        )
    return StripDecodeResult(uncompressed_bytes=written)


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
    """
    while data:
        max_len = min(_RANGE_CHUNK_BYTES, bound - written + 1)
        try:
            produced = decompressor.decompress(data, max_len)
        except zlib.error as exc:
            raise _object_error(
                "gzip transport stream is corrupt: decompression failed; re-upload the object"
            ) from exc
        if produced:
            writer.write(produced)
            written += len(produced)
            if written > bound:
                raise _object_error(
                    "decompressed output exceeds the declared uncompressed_size bound "
                    f"({bound} bytes): the object is not a valid gzip of that size (a gzip bomb "
                    "or a wrong uncompressed_size); re-declare with the correct uncompressed_size "
                    "or upload the correct object"
                )
        data = decompressor.unconsumed_tail
    return written
