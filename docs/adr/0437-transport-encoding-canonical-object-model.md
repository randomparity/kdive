# ADR 0437 — Transport-encoding vs payload-format: the canonical-object model for agent uploads

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0048](0048-external-build-artifact-ingestion.md) (the agent-upload
  transport, the declaration manifest, and the owner-scoped upload window),
  [ADR-0104](0104-chunked-external-upload-reassembly.md) (the chunked/multipart mechanism this
  keeps disjoint from encoding), [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md)
  (the #743 rootfs install path whose plain-SHA-256 transport verify this stays consistent with),
  [ADR-0436](0436-reject-chunked-system-rootfs-upload.md) (the per-owner `allow_chunks` capability
  this mirrors for `accepts_encoding`).
- **Part of:** epic #1508 (transparent transport-encoding for agent-uploaded objects); this is
  Sub 1 (#1509) — the model, declaration validation, and shared decode utility. The rootfs
  consumer is Sub 2 (#1510); remote parity + agent-surface docs are Sub 3 (#1511).

## Context

Agent-uploaded artifacts are capped by the S3 single-PUT ceiling (`SINGLE_PUT_MAX_BYTES` =
5 GiB, `src/kdive/artifacts/uploads.py`). A realistic custom debug rootfs (distro +
kernel-debuginfo + drgn/crash + symbols) routinely exceeds 5 GiB uncompressed yet compresses
well below it. Today an agent has no way to declare that the bytes it PUTs are a compressed
wrapper around a larger canonical object: the manifest declaration carries only `(name, sha256,
size_bytes[, chunks])`, and every consumer treats the stored bytes as the artifact verbatim.

The two size mechanisms already in the tree do not solve this:

- **Chunked/multipart** (ADR-0104) raises the *transport* ceiling to `KDIVE_MAX_UPLOAD_BYTES`
  (50 GiB) but reassembles into an object whose only stored checksum is a composite
  `<base64>-<N>`, which the #743 rootfs install path's plain `sha256(body) ==
  head.checksum_sha256` verify (ADR-0434) can never satisfy — so ADR-0436 rejects a chunked
  rootfs at declaration.
- **qcow2-internal compression** (`qemu-img convert -c`) couples the workaround to the qcow2
  container and its per-cluster ratios, and does nothing for a non-qcow2 payload.

What is missing is a first-class notion that the uploaded bytes may be a **transport wrapper**
distinct from the payload they carry.

## Decision

Introduce a **transport-encoding vs payload-format** model. An agent may declare a transport
`encoding` on an upload; kdive strips it on download to recover the **canonical object**, then
the existing per-artifact format validation runs on that canonical object. Sub 1 lands the model,
the declaration validation, the manifest persistence, and the shared decode utility. It wires **no
consumer** — the rootfs consumer is Sub 2.

### 1. `encoding` is a transport wrapper, semantically distinct from format

`encoding` describes how the bytes are framed **in transit and at rest in the store**, not what
the payload *is*. `encoding: "gzip"` means "the stored object is a gzip stream; the canonical
object is what you get after gunzip." Absent `encoding` (or the explicit sentinel `"identity"`)
means the stored bytes already *are* the canonical object — byte-for-byte today's behavior. Format
validation (qcow2 magic for a rootfs, ELF for a vmlinux, the combined-tar shape for a kernel) is a
property of the **canonical** object and is unchanged by this model; it simply runs after the strip.
gzip is the only non-identity codec in the first cut; the field is defined with room for `zstd`/`xz`
later without a schema change.

### 2. Declaration fields and validation

`ManifestEntry` gains `encoding: str | None = None` (absent → identity) and `uncompressed_size:
int | None = None` (canonical-object size in bytes). `_validate_artifact_declarations`
(`src/kdive/mcp/tools/catalog/artifacts/uploads.py`) enforces, at declaration time and before any
presign:

- **`uncompressed_size` required with `encoding`.** A non-identity `encoding` without a positive
  integer `uncompressed_size` is rejected — the size bound is what makes the fail-fast and the
  gzip-bomb guard possible.
- **Unknown codec rejected.** Any `encoding` outside `{gzip, identity}` is rejected. `identity`
  normalizes to "no encoding".
- **`uncompressed_size` only with an encoding.** `uncompressed_size` present on an
  identity/absent-encoding declaration is rejected as meaningless, rather than silently ignored.
- **`encoding` + `chunks` rejected.** Encoded uploads are single-PUT only. This keeps the transport
  hash a plain single-object sha256 and sidesteps the composite checksum of a reassembled multipart
  object (`src/kdive/build_artifacts/validation.py`, the same integrity reason as ADR-0436).
- **Per-owner `accepts_encoding` capability.** `_UploadOwnerSpec` gains `accepts_encoding: bool =
  False`, mirroring ADR-0436's `allow_chunks`. Only an owner with a registered decompressing
  consumer accepts a non-identity `encoding`; every other owner rejects it at declaration (no
  accept-then-ignore). First cut: **systems accepts, runs rejects** — runs' build artifacts are
  already format-validated and no concrete >5 GiB build artifact needs transport-gzip today
  (an "accept" would be a speculative consumer).
- **Canonical-object cap fail-fast.** `_UploadOwnerSpec` gains `uncompressed_cap: int`; the shared
  validator rejects `uncompressed_size > uncompressed_cap` before presign, so an over-cap image is
  refused before any bytes move. **Both** owner caps live here in the shared validator so a future
  consumer never edits it. Systems' cap is 50 GiB (aligned with the `KDIVE_MAX_UPLOAD_BYTES`
  per-artifact ceiling — the canonical object is bound by the same ceiling whether it arrives raw
  or transport-gzipped); runs' cap is the 5 GiB single-PUT ceiling (runs has no decompressing
  consumer, so its cap only ever gates a future opt-in). The existing compressed-`size_bytes` ≤
  5 GiB single-PUT check is unchanged and still applies to the transport bytes.

### 3. Transport-checksum semantics (fixed, not an open question)

`head.checksum_sha256` is over the **stored (compressed) bytes**, and the presigned PUT signs that
same compressed sha256 — so the transport hash verifies the compressed bytes and stays consistent
with the signed PUT and with the ADR-0434 rootfs verify. The decompressed (canonical) side is
guarded by the gzip CRC/ISIZE trailer plus the `uncompressed_size` bound, not by a second stored
hash.

### 4. Shared streaming strip-decode utility

A new module `src/kdive/artifacts/transport_encoding.py` holds the codec constants and
`strip_gzip_to_writer(store, request, writer)`. Given a store key, the compressed size, the
expected compressed sha256, and the `uncompressed_size` bound, it:

1. reads the compressed object in sequential **ranged** GETs (never buffers the whole object);
2. gunzips each range into the caller's `writer` (streaming, so the multi-GiB canonical object is
   never held in RAM), bounding per-call output to protect memory;
3. hashes the **compressed** bytes as they stream (transport verify);
4. **fails closed** the instant decompressed output would exceed the `uncompressed_size` bound
   (gzip-bomb guard) — it never expands a bomb.

It is modeled on the bounded gunzip `_decompress_bounded`
(`src/kdive/build_artifacts/validation.py`) but is writer-oriented and consumer-agnostic: it takes
an injected ranged-read store and a writer, wires no consumer, and raises a `CONFIGURATION_ERROR`
`CategorizedError` with a self-correcting message on a bomb, a corrupt/truncated gzip stream, or a
transport-hash mismatch. Sub 2 consumes it to stage the rootfs base; the caller owns atomic staging
so a raised error discards the partial output.

### Verify ordering

**Stream-decompress-while-hashing (single pass), then fail-closed on a transport-hash mismatch at
the end.** During the stream the gzip CRC (verified by `zlib` at the trailer) and the
`uncompressed_size` bomb-bound guard the decompressed side, so a corrupt or over-bound stream is
rejected before the canonical object is completed; the compressed-bytes hash is compared to the
expected transport sha256 only at end-of-stream. Rationale: a two-pass "hash first, then
decompress" ordering would double the ranged reads of a multi-GiB object for no added safety —
gzip's own CRC already makes a silently-corrupt-but-CRC-valid stream astronomically unlikely, and
the bomb-bound already caps the write.

## Consequences

- A new, generic upload capability: any owner can opt into transport-gzip by registering a
  decompressing consumer and flipping `accepts_encoding`; the declaration validation, cap
  fail-fast, manifest persistence, and decode utility are all owner-agnostic and land once, here.
- No schema and **no DB migration**: the two fields ride the existing `upload_manifests` JSONB
  (`_entry_payload` + its parser), defaulting absent → identity so in-flight/pre-existing manifests
  deserialize cleanly; manifests are ephemeral (reaped). No new MCP tool or RBAC surface — the
  fields ride the existing CONTRIBUTOR-gated `create_*_upload` tools.
- Backward compatible: a declaration with no `encoding` behaves byte-for-byte as before.
- Sub 1 ships the utility with tests but **no wiring**. Until Sub 2 lands, systems `accepts_encoding`
  is `True` at declaration but nothing consumes a gzip rootfs — an agent that PUTs one and provisions
  before Sub 2 would still hit the verbatim-stage failure. The declaration surface is intentionally
  landed first so Sub 2 is a pure consumer change.

## Considered & rejected

- **Fold encoding into the payload format.** Rejected: it conflates "how the bytes travel" with
  "what the payload is", forcing every format validator to learn every codec. Keeping `encoding` a
  transport wrapper stripped to a canonical object lets the existing format validators stay
  format-only.
- **A second stored hash over the decompressed bytes.** Rejected: the store only ever holds the
  compressed object, and the signed PUT binds the compressed sha256. gzip's CRC + the
  `uncompressed_size` bound already guard the decompressed side; a second hash would require either
  a client-declared canonical hash we cannot bind to the PUT, or a full extra pass.
- **Accept `encoding` on every owner and ignore it where unconsumed.** Rejected: accept-then-ignore
  is a phantom feature. An owner with no decompressing consumer rejects a non-identity `encoding`
  at declaration, so the surface never over-promises.
- **Two-pass verify (hash the whole object, then decompress).** Rejected: it doubles ranged reads
  of a multi-GiB object with no safety gain over gzip-CRC + bomb-bound during a single pass.
