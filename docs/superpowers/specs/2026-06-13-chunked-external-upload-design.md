# Chunked external-build uploads (>5 GiB) — design

- **Status:** Draft
- **Date:** 2026-06-13
- **Goal:** Let an agent upload an external build artifact larger than the real-S3
  single-PUT ceiling (5 GiB) by splitting it client-side into ≤5 GiB chunks that the
  server reassembles into one object at finalize, so large `vmlinux`/debuginfo uploads
  work against real S3, not only MinIO.
- **Depends on:** [ADR-0048](../../adr/0048-external-build-artifact-ingestion.md) (the
  external-build lane: `create_upload` presigned PUTs, the persisted upload manifest, the
  synchronous `complete_build`, the prefix reaper) and the object store
  ([ADR-0017](../../adr/0017-object-store-client-interface.md) /
  [ADR-0013](../../adr/0013-object-store-layout-retention.md)).
- **ADR:** [ADR-0104](../../adr/0104-chunked-external-upload-reassembly.md) (the decisions
  this spec settles).
- **Issue:** [#112](https://github.com/randomparity/kdive/issues/112).

## 1. Problem

`artifacts.create_upload` mints a single presigned PUT per declared artifact. A single PUT
caps at **5 GiB** on real S3 (the documented single-object-PUT limit). ADR-0048-review
hardening lowered `KDIVE_MAX_UPLOAD_BYTES` to 5 GiB so a minted PUT is always within that
limit — which lowered the ceiling rather than raising the capability. A `vmlinux` or
debuginfo artifact above 5 GiB is rejected up front with `configuration_error`
(`size_out_of_range`). Large uploads therefore work only against MinIO (whose single-PUT
limit is higher), not real S3 — exactly the deployment the platform targets.

This design adds a **chunked upload lane** for an external Run's build artifacts: the agent
splits an artifact into ordered ≤5 GiB chunks, uploads each via an ordinary presigned PUT,
and `runs.complete_build` reassembles the chunks into the single final object the install /
debug planes already read — without streaming gigabytes through the server.

## 2. Scope

In scope (the Run/build-artifact lane only):

- `src/kdive/config/core_settings.py` — raise `KDIVE_MAX_UPLOAD_BYTES` default from 5 GiB
  to 50 GiB (the per-artifact declared-size cap; still config-overridable).
- `src/kdive/provider_components/uploads.py` — `ManifestEntry` gains an optional ordered
  `chunks: tuple[ChunkEntry, ...] | None`; a `ChunkEntry` is `(sha256, size_bytes)`.
- `src/kdive/db/upload_manifest.py` — serialize/deserialize the optional `chunks` list in
  the existing **JSONB** `manifest` column. **No DDL migration** — the column is schemaless.
- `src/kdive/mcp/tools/catalog/artifacts/uploads.py` — accept a chunked artifact
  declaration, validate the chunk constraints (§5), and mint one presigned PUT per chunk
  (keyed `<name>.partNNNN`).
- `src/kdive/store/objectstore.py` — four multipart primitives:
  `create_multipart_upload`, `upload_part_copy`, `complete_multipart_upload`,
  `abort_multipart_upload` (server-side copy reassembly; no bytes through the server).
- `src/kdive/provider_components/build_validation.py` (and a small reassembly helper) —
  for a chunked entry, verify each chunk's `(size, sha256)` by HEAD, reassemble into the
  final key, then run the existing magic + ranged `build_id` validation on the final object;
  skip the whole-object checksum comparison (the final multipart object exposes only a
  composite checksum — see §4).
- `src/kdive/mcp/tools/lifecycle/runs/build.py` — `complete_build` orchestrates reassembly
  before validation, defers manifest deletion past chunk cleanup, and best-effort deletes
  chunk objects after commit.
- `src/kdive/reconciler/uploads.py` — generalize the reaper so a manifest lingering past
  its deadline is swept whether its owner is **pre-finalize** (true abandon) or
  **finalized with incomplete chunk cleanup** (the backstop for a failed post-commit
  delete). The per-object "no committed `artifacts` row → delete" predicate is unchanged.

Out of scope (stated so reviews do not assume otherwise):

- **System/rootfs chunking.** The System-owned rootfs `create_upload` stays single-PUT
  (≤5 GiB). The provisioning plane's manifest lifecycle is untouched. A >5 GiB rootfs is a
  separate follow-up; nothing here regresses it.
- **Native S3 multipart upload (MPU) as the *transport*.** Parts do not stream straight
  into the final object; the agent uploads independent chunk objects and the server
  reassembles. See ADR-0104 "Considered & rejected."
- **Server-side whole-object re-hash.** The final object is not downloaded to re-derive its
  SHA-256; that re-derivation stays deferred to whenever a plane downloads the artifact
  anyway (the same bounded-trust treatment ADR-0048 gives `build_id`).

## 3. The agent contract

An artifact declaration is either **single** (today's shape) or **chunked**:

```jsonc
// single — unchanged, must be <= 5 GiB
{ "name": "vmlinux", "sha256": "<b64>", "size_bytes": 4294967296 }

// chunked — size_bytes is the advisory whole-object total; chunks is ordered
{ "name": "vmlinux", "sha256": "<b64 whole-object, advisory>",
  "size_bytes": 8589934592,
  "chunks": [
    { "sha256": "<b64 chunk 0>", "size_bytes": 5368709120 },
    { "sha256": "<b64 chunk 1>", "size_bytes": 3221225472 } ] }
```

`create_upload` returns **one upload item per chunk** for a chunked artifact (and one item
per artifact for a single one), each carrying the chunk's presigned `upload_url`, the signed
`x-amz-checksum-sha256` header, and `data.artifact_name` + `data.part_number` so the agent
can match URL → chunk. The agent PUTs each chunk to its URL, then calls
`runs.complete_build` exactly as today (no new tool, no ETag round-trip).

Chunk object keys are `{tenant}/runs/<run_id>/<name>.partNNNN` (1-based, zero-padded). The
reassembled object lands at the existing `{tenant}/runs/<run_id>/<name>`, so install / debug
read an unchanged key. The `.partNNNN` suffix cannot collide with another allowlisted
artifact name.

## 4. Integrity model: per-chunk pins, advisory whole-object hash

ADR-0048 §4 anchored integrity on the finalize-time check
`head(key).checksum_sha256 == manifest.sha256` (the whole-object SHA-256). That check
**cannot survive chunking**: the reassembled object is a multipart object, and S3's
multipart SHA-256 is *composite-only* (a checksum-of-checksums with a `-N` suffix), never
the whole-object SHA-256. (AWS added full-object multipart checksums in 2025, but only for
CRC32/CRC32C/CRC64NVME; SHA-256 stays composite. MinIO matches this.)

The integrity anchor therefore moves, for chunked artifacts, to **per-chunk SHA-256 pins**:

1. **PUT-time** — each chunk is uploaded via the existing `presign_put`, which signs the
   chunk's `x-amz-checksum-sha256` into the URL; the store rejects a chunk body whose
   checksum disagrees. This is the *same* binding single uploads already rely on, applied
   per chunk — no new PUT-time integrity story.
2. **Finalize-time** — before reassembly, `complete_build` HEADs each chunk object and
   confirms its stored `(size_bytes, checksum_sha256)` equals the manifest chunk entry. A
   missing or mismatched chunk fails with `configuration_error` / `build_failure` before any
   reassembly happens.

The reassembled object is created **without** a server-side checksum algorithm, so its
`head().checksum_sha256` is `None`; the chunked validation path skips the whole-object
checksum comparison (it was already enforced per chunk). The declared whole-object `sha256`
is recorded as **advisory** — re-derivable later when a plane downloads the artifact, the
same bounded-trust treatment ADR-0048 applies to `build_id`. The magic checks and the ranged
`.note.gnu.build-id` extraction run on the reassembled object and are unaffected by the
composite checksum (they are byte-range reads).

## 5. Declaration validation

`_validate_artifact_declarations` gains the chunk rules. With `cap = KDIVE_MAX_UPLOAD_BYTES`
(default 50 GiB) and the constants `SINGLE_PUT_MAX_BYTES = 5 GiB`, `MAX_PART_BYTES = 5 GiB`,
`MIN_PART_BYTES = 5 MiB`:

- **Single (no `chunks`):** `0 < size_bytes <= min(SINGLE_PUT_MAX_BYTES, cap)`. (The
  single-PUT physical ceiling still binds; the 50 GiB cap only opens with chunks.)
- **Chunked (`chunks` present):**
  - `1 <= len(chunks) <= 10_000` (the MPU part-count limit, trivially met).
  - each chunk `0 < size_bytes <= MAX_PART_BYTES`.
  - every **non-final** chunk `size_bytes >= MIN_PART_BYTES` (the `UploadPartCopy` part-size
    floor; the final chunk may be smaller).
  - `sum(chunk.size_bytes) == size_bytes` and `0 < size_bytes <= cap`.
  - a single-element `chunks` list whose one chunk is `<= SINGLE_PUT_MAX_BYTES` is legal but
    pointless; it is accepted (uniform path) rather than special-cased.
- **`effective_config`** keeps its 1 MiB cap and is never chunked (a `chunks` list on it is
  `size_out_of_range`).

Any violation returns `configuration_error` with a specific `reason`
(`size_out_of_range`, `chunk_too_small`, `chunk_size_mismatch`, `too_many_chunks`,
`bad_artifact_declaration`) before any URL is minted or manifest persisted.

## 6. Reassembly at finalize

For each **chunked** artifact, inside `complete_build` (still synchronous, ADR-0048 §3),
before the finalize DB transaction:

1. HEAD + verify every chunk object against the manifest (§4 step 2).
2. `create_multipart_upload(final_key, sensitivity=SENSITIVE, retention_class="build")` →
   `upload_id`.
3. For each chunk in order, `upload_part_copy(final_key, upload_id, part_number=i+1,
   source_key=chunk_key)` → part ETag (a server-side copy; no bytes transit the server).
4. `complete_multipart_upload(final_key, upload_id, parts)`.
5. On **any** failure in 2–4, `abort_multipart_upload(final_key, upload_id)` and return the
   typed error; the Run stays `CREATED`, so the abandoned-upload reaper backstops the chunk
   objects (and any half-written final object) under the prefix.

Then the existing validation runs on the now-single final keys (magic + ranged `build_id`;
chunked entries skip the whole-object checksum), and the existing finalize DB transaction
writes the `artifacts` row for the final object, flips `created → succeeded`, and records the
step ledger.

**Chunk cleanup (the leak boundary).** After the finalize commit, `complete_build`
best-effort deletes each chunk object, then best-effort deletes the manifest. If cleanup
fully succeeds the manifest is gone (as today). If a delete fails, the manifest **lingers**;
the reaper (§7) reclaims the leftover chunks once the deadline passes. Cleanup is idempotent
(`delete` of an absent key is a no-op), so a reaper/finalize race is harmless.

## 7. Reaper generalization

`repair_abandoned_uploads` today sweeps a manifest past its deadline only when its owner is
still **pre-finalize** (`CREATED` Run / `DEFINED` System). That gate makes a *succeeded*
Run's leftover chunks (from a failed post-commit cleanup) unreachable — a storage leak.

The reaper is generalized so the obligation is **"a manifest past its deadline"**, swept
whether the owner is pre-finalize *or* finalized:

- The candidate query drops the owner-state predicate and selects any manifest with
  `deadline < now()`.
- `reap_one_owner` re-reads the manifest under the per-owner advisory lock, lists the
  prefix, and deletes only objects with **no committed `artifacts` row** (unchanged
  predicate — the committed reassembled object and any committed rootfs are exempt), then
  deletes the manifest.

This is safe because the per-object no-row predicate, not the owner state, is the live-data
guard, and the per-owner lock + `deadline < now()` still serialize against an in-flight
finalize before the deadline. The change is additive: a true abandon (pre-finalize owner)
sweeps exactly as before; the new case is a finalized owner whose only uncommitted prefix
objects are dead chunks.

## 8. Object-store primitives

Added to `ObjectStore` (synchronous boto3; async callers offload via `asyncio.to_thread`):

- `create_multipart_upload(key, *, sensitivity, retention_class) -> str` — `CreateMultipart
  Upload` with the object metadata set here (it cannot be set at completion); returns the
  `upload_id`.
- `upload_part_copy(key, upload_id, *, part_number, source_key) -> str` — `UploadPartCopy`
  from an existing chunk object into part `part_number`; returns the part ETag. No checksum
  algorithm is set, so the final object carries an ETag but no whole-object checksum.
- `complete_multipart_upload(key, upload_id, parts: Sequence[tuple[int, str]]) -> str` —
  `CompleteMultipartUpload` with the ordered `(part_number, etag)` list; returns the final
  ETag.
- `abort_multipart_upload(key, upload_id) -> None` — `AbortMultipartUpload`; idempotent
  best-effort cleanup on a mid-reassembly failure.

All map a `BotoCoreError`/`ClientError` to `INFRASTRUCTURE_FAILURE` via the existing
`_infrastructure_error`.

## 9. Verification

Unit / service (run in normal CI against MinIO + Postgres):

- **Declaration validation** — single >5 GiB rejected `size_out_of_range`; chunked total
  >cap rejected; non-final chunk <5 MiB rejected `chunk_too_small`; `sum(chunks) != size`
  rejected `chunk_size_mismatch`; chunked `effective_config` rejected; well-formed chunked
  accepted with N part URLs minted at `.partNNNN`.
- **Manifest round-trip** — a chunked entry persists and reloads its ordered `chunks` list
  through the JSONB column.
- **Reassembly happy path** — a fake store records `create/upload_part_copy/complete` calls;
  `complete_build` HEAD-verifies chunks, reassembles in order, validates the final object,
  writes one `artifacts` row at the final key, flips the Run `succeeded`.
- **Reassembly failure** — a chunk HEAD mismatch fails before any MPU call; an
  `upload_part_copy` error triggers `abort_multipart_upload` and leaves the Run `CREATED`.
- **Reaper backstop** — a succeeded Run with a lingering past-deadline manifest and leftover
  chunk objects: the reaper deletes the chunks (no row) but **not** the reassembled final
  object (has a row), then deletes the manifest. A pre-finalize abandon still reaps exactly
  as before (regression-guard the existing behavior).
- **Cleanup idempotency** — a finalize whose post-commit chunk delete fails still commits the
  Run; re-deleting an absent chunk is a no-op.

`live_stack` / operator (MinIO in CI; the real-S3 assertion is operator-run, like ADR-0048
§7's checksum item):

- Chunked reassembly produces a final object whose ranged reads (bzImage/ELF magic) succeed,
  proving the multipart-composite object is byte-range readable.
- A note records that the real-S3-only assertion — "a single PUT of a >5 GiB body is rejected
  by S3, and the chunked lane succeeds where it does" — runs against real S3, since MinIO's
  higher single-PUT limit cannot reproduce the rejection in CI.

## 10. Consequences

- Real-S3 deployments can ingest external build artifacts up to 50 GiB; the single-PUT lane
  is unchanged for ≤5 GiB artifacts.
- The integrity anchor for chunked artifacts is per-chunk SHA-256 (PUT-pinned +
  HEAD-confirmed); the whole-object SHA-256 becomes advisory until a download re-derives it.
- Four object-store multipart primitives are added; reassembly is server-side copy, so no
  artifact bytes transit the server and `complete_build` stays synchronous.
- The reaper's sweep obligation generalizes from "pre-finalize owner" to "manifest past
  deadline," closing the post-commit chunk-cleanup leak; the per-object no-row safety
  predicate is unchanged.
- A transient ~2× storage exists for a chunked artifact between reassembly and chunk cleanup,
  bounded by the upload TTL / reaper deadline.
- CI (MinIO) cannot reproduce the real-S3 single-PUT rejection that motivates the feature;
  that one assertion is operator-run, mirroring ADR-0048 §7.
