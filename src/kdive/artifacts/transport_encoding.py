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


def _decode_error(detail: str) -> CategorizedError:
    return CategorizedError(detail, category=ErrorCategory.CONFIGURATION_ERROR)


def strip_gzip_to_writer(
    store: RangedReadStore, request: StripDecodeRequest, writer: IO[bytes]
) -> StripDecodeResult:
    """Stream-gunzip a gzip transport object into ``writer``, bounded and hash-verified.

    Single pass over the compressed object via sequential ranged reads: each range is gunzipped into
    ``writer`` (never buffering the whole canonical object) while the *compressed* bytes are hashed.
    Decompressed output is capped at ``request.uncompressed_size`` — the instant it would exceed the
    bound the call fails closed (gzip bomb), so a bomb is never expanded. At end-of-stream the gzip
    trailer must have been reached (``zlib`` verifies its CRC/ISIZE) and the compressed hash must
    match ``request.expected_sha256`` (transport verify). Any of a bomb, a corrupt/truncated gzip
    stream, or a hash mismatch raises a ``CONFIGURATION_ERROR`` ``CategorizedError`` with a
    self-correcting message; the caller owns atomic staging so a raised error discards the partial
    output already written.

    Args:
        store: A ranged-read store over the compressed object.
        request: The key, compressed size, expected compressed hash, and uncompressed-size bound.
        writer: A binary sink the canonical (decompressed) bytes stream into.

    Returns:
        A :class:`StripDecodeResult` with the number of decompressed bytes written.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on a gzip bomb, a corrupt or truncated gzip
            stream, or a transport-checksum mismatch.
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
        raise _decode_error(
            "gzip transport stream is truncated: it ended before the gzip trailer, so the "
            "canonical object is incomplete; re-upload the full object"
        )
    actual = base64.b64encode(hasher.digest()).decode("ascii")
    if actual != request.expected_sha256:
        raise _decode_error(
            "transport checksum mismatch: the stored object's SHA-256 does not match the signed "
            "upload; re-upload the object (do not retry the same corrupt bytes)"
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
            raise _decode_error(
                "gzip transport stream is corrupt: decompression failed; re-upload the object"
            ) from exc
        if produced:
            writer.write(produced)
            written += len(produced)
            if written > bound:
                raise _decode_error(
                    "decompressed output exceeds the declared uncompressed_size bound "
                    f"({bound} bytes): the object is not a valid gzip of that size (a gzip bomb "
                    "or a wrong uncompressed_size); re-declare with the correct uncompressed_size "
                    "or upload the correct object"
                )
        data = decompressor.unconsumed_tail
    return written
