# Design — Core transport-encoding model, declaration validation & shared decode utility (#1509)

- **Issue:** #1509 (epic #1508 Sub 1). **ADR:** [ADR-0437](../adr/0437-transport-encoding-canonical-object-model.md).
- **Migration:** none (fields ride the ephemeral `upload_manifests` JSONB).
- **Scope:** the model + declaration validation + manifest persistence + a shared, unwired decode
  utility. **Out of scope:** the rootfs consumer (Sub 2 / #1510), any provider decompression, other
  codecs, storage-cost accounting.

## Problem

Agent uploads are capped at the 5 GiB S3 single-PUT ceiling. A debug rootfs exceeds that
uncompressed but compresses below it. kdive has no notion that the uploaded bytes may be a
compressed transport wrapper around a larger canonical object. See ADR-0437 for the full model.

## Requirements → design

### 1. Declaration fields — `src/kdive/artifacts/uploads.py`

`ManifestEntry` (a `NamedTuple`) gains two optional trailing fields:

```python
encoding: str | None = None          # transport codec; None / "identity" ⇒ identity (verbatim)
uncompressed_size: int | None = None # canonical-object size in bytes; required with a non-identity encoding
```

Codec constants live in the new `transport_encoding` module (below) and are imported by the
validator. `GZIP_ENCODING = "gzip"`, `IDENTITY_ENCODING = "identity"`,
`KNOWN_ENCODINGS = frozenset({GZIP_ENCODING, IDENTITY_ENCODING})`.

### 2. Declaration validation — `src/kdive/mcp/tools/catalog/artifacts/uploads.py`

`_UploadOwnerSpec` gains `accepts_encoding: bool = False` and `uncompressed_cap: int` (mirroring the
ADR-0436 `allow_chunks` per-owner pattern). `_SYSTEM_UPLOAD`: `accepts_encoding=True`,
`uncompressed_cap = _SYSTEM_UNCOMPRESSED_CAP` (50 GiB). `_RUN_UPLOAD`: `accepts_encoding=False`,
`uncompressed_cap = _RUN_UNCOMPRESSED_CAP` (5 GiB single-PUT). Both caps are module constants in the
shared validator so a future consumer never edits it.

`_validate_artifact_declarations` gains keyword params `accepts_encoding: bool` and
`uncompressed_cap: int`; `_create_upload` passes `spec.accepts_encoding` / `spec.uncompressed_cap`.
A new `_validate_encoding(object_id, declaration, *, accepts_encoding, uncompressed_cap, has_chunks)`
runs after the base-field validation and before the chunk branch, returning `(encoding,
uncompressed_size)` or a `ToolResponse` rejection. Rules (all self-correcting, ADR-0166 style):

| condition | `data.reason` |
|---|---|
| `encoding` not a string in `KNOWN_ENCODINGS` | `unknown_encoding` |
| identity/absent encoding but `uncompressed_size` present | `uncompressed_size_without_encoding` |
| non-identity encoding, owner `accepts_encoding` False | `encoding_not_supported` |
| non-identity encoding + `chunks` present | `encoding_with_chunks` |
| non-identity encoding, `uncompressed_size` missing / not a positive int | `uncompressed_size_required` |
| non-identity encoding, `uncompressed_size > uncompressed_cap` | `uncompressed_size_over_cap` |

`identity` normalizes to `None`. On accept, the single-PUT branch builds
`ManifestEntry(..., encoding=encoding, uncompressed_size=uncompressed_size)`. The existing
compressed-`size_bytes` ≤ `min(SINGLE_PUT_MAX_BYTES, artifact_cap)` check is unchanged and still
binds the transport bytes. A chunked entry never carries an encoding (the combo is rejected).

### 3. Manifest persistence — `src/kdive/artifacts/upload_manifest.py`

`_entry_payload` adds `encoding`/`uncompressed_size` to the JSON only when `encoding is not None`.
`_entry_from_payload` reads them with `payload.get(...)`, defaulting absent → `None` so pre-existing
manifests deserialize as identity. No migration (manifests are ephemeral, reaped).

### 4. Shared decode utility — new `src/kdive/artifacts/transport_encoding.py`

```python
class RangedReadStore(Protocol):
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...

@dataclass(frozen=True)
class StripDecodeRequest:
    key: str
    compressed_size: int    # total stored transport bytes to range over
    expected_sha256: str    # base64 SHA-256 of the compressed bytes (transport verify)
    uncompressed_size: int  # canonical-object bound (gzip-bomb guard)

class StripDecodeResult(NamedTuple):
    uncompressed_bytes: int

def strip_gzip_to_writer(store: RangedReadStore, request: StripDecodeRequest, writer: IO[bytes]) -> StripDecodeResult: ...
```

Single pass, modeled on `_decompress_bounded` but writer-oriented: sequential ranged GETs of
`_RANGE_CHUNK_BYTES`; gunzip via `zlib.decompressobj(16 + zlib.MAX_WBITS)`, feeding
`unconsumed_tail` and bounding per-call output to `_RANGE_CHUNK_BYTES` and total output to
`uncompressed_size + 1` (the +1 sentinel triggers the bomb guard the instant output would exceed
the bound); write each produced block to `writer`; update a `hashlib.sha256` over the compressed
bytes. Raise `CategorizedError(CONFIGURATION_ERROR)` on: output > bound (`gzip bomb`), a corrupt
gzip stream (`zlib.error`), a truncated stream (input exhausted before `decompressor.eof`), or a
transport-hash mismatch (`base64(sha256(compressed)) != expected_sha256`, checked at end). The
utility wires no consumer; the caller owns atomic staging so a raised error discards partial output.

## Testing (behavior)

- **Declaration rejects:** encoding without `uncompressed_size`; unknown codec;
  `encoding`+`chunks`; over-cap `uncompressed_size`; encoding on the runs owner;
  `uncompressed_size` without encoding.
- **Declaration accepts:** `encoding`+`uncompressed_size` ≤ cap on systems (entry carries the two
  fields); a plain no-encoding declaration unchanged (entry fields default `None`).
- **Manifest round-trip:** an encoded entry serializes and deserializes intact; a pre-existing
  payload without the keys deserializes to `encoding=None, uncompressed_size=None`.
- **Utility:** streams a multi-range object (asserts multiple ranged reads, no single whole-object
  read, per-read length ≤ `_RANGE_CHUNK_BYTES`); verifies the compressed hash; rejects a crafted
  gzip bomb (decompressed output > bound); rejects a wrong expected hash; rejects a truncated
  stream. A fake in-memory `RangedReadStore` backs the tests.

## Rollback

Pure additive: the fields default to identity and the utility is unwired, so reverting the branch
restores byte-for-byte prior behavior. No migration to reverse.
