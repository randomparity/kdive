# ADR 0104 — Chunked external-build uploads with server-side reassembly

- **Status:** Proposed
- **Date:** 2026-06-13
- **Depends on:** [ADR-0048](0048-external-build-artifact-ingestion.md) (the external-build
  lane: `create_upload` presigned PUTs, the persisted upload manifest, the synchronous
  `complete_build`, the prefix reaper, the agent-declared `build_id` trust point),
  [ADR-0017](0017-object-store-client-interface.md) /
  [ADR-0013](0013-object-store-layout-retention.md) (the object store and key layout).
- **Spec:** [`../superpowers/specs/2026-06-13-chunked-external-upload-design.md`](../superpowers/specs/2026-06-13-chunked-external-upload-design.md)
- **Issue:** [#112](https://github.com/randomparity/kdive/issues/112).

## Context

`artifacts.create_upload` mints one presigned PUT per artifact. A single PUT caps at 5 GiB
on real S3, so ADR-0048-review hardening set `KDIVE_MAX_UPLOAD_BYTES` to 5 GiB — a minted
PUT is always within the limit, but a `vmlinux`/debuginfo artifact above 5 GiB is now
rejected up front (`size_out_of_range`). Large uploads work only against MinIO (higher
single-PUT limit), not the real S3 the platform targets. #112 asks for a path that ingests
artifacts larger than 5 GiB.

Two transports were on the table: native S3 multipart upload (the agent uploads parts that
stream into the final object), and client-side split into independent ≤5 GiB chunk objects
that the server reassembles. Both produce a **multipart** final object, whose SHA-256 the
store exposes only as a *composite* checksum (`-N` suffix), never the whole-object hash — so
either way ADR-0048 §4's `head().checksum_sha256 == manifest.sha256` finalize check must be
replaced by a per-chunk integrity model.

## Decision

### 1. Client-side split + server-side reassembly, not native MPU transport

The agent splits an artifact into ordered ≤5 GiB chunks and uploads each as an independent
object via the **existing** `presign_put` single-PUT path (each chunk checksum-pinned in the
signed URL, exactly as a single artifact is today). `runs.complete_build` reassembles the
chunks into the one final object the install / debug planes already read, using
`CreateMultipartUpload` + `UploadPartCopy` (server-side copy) + `CompleteMultipartUpload` —
no artifact bytes transit the server.

This keeps the new, backend-sensitive behavior confined to the already-synchronous
`complete_build` (ADR-0048 §3), reuses the single-PUT path already verified against MinIO,
and — decisively — keeps the abandoned-upload leak closed by the **existing prefix reaper**:
chunk objects are plain objects under the owner prefix, swept by the same "no committed
`artifacts` row" predicate. Native MPU would instead leave an in-progress multipart upload
that `ListObjectsV2` cannot see, forcing a second `ListMultipartUploads` + `Abort` reaper
path, a persisted `upload_id` session, and a new agent round-trip to report part ETags.

### 2. Integrity: per-chunk SHA-256 pins; whole-object hash advisory

For a chunked artifact, integrity is anchored per chunk: each chunk's `x-amz-checksum-sha256`
is signed into its presigned PUT (PUT-time store rejection of a mismatched body), and
`complete_build` HEAD-confirms each chunk's stored `(size, checksum)` against the persisted
manifest before reassembly. The reassembled object is created **without** a server-side
checksum algorithm, so its `head().checksum_sha256` is `None`; the chunked validation path
skips the whole-object checksum comparison. The agent-declared whole-object `sha256` is
recorded as advisory metadata, re-derivable when a plane downloads the artifact — the same
bounded, documented trust treatment ADR-0048 gives the declared `build_id`. Magic checks and
the ranged `.note.gnu.build-id` extraction run on the reassembled object unchanged (byte-range
reads, unaffected by the composite checksum).

### 3. Manifest carries chunks in JSONB; no migration

`ManifestEntry` gains an optional ordered `chunks: (sha256, size_bytes)…`. The
`upload_manifests.manifest` column is JSONB, so the chunk list is persisted in place with no
DDL migration; the `artifacts` row stays write-once and unchanged.

### 4. Reassembly is synchronous and abort-safe

`complete_build` HEAD-verifies all chunks, then `Create`/`UploadPartCopy×N`/`Complete`s the
final object; any failure in that sequence triggers `AbortMultipartUpload` and returns a
typed error with the Run left `CREATED`, so the reaper backstops the chunks and any
half-written final object. Object metadata (sensitivity, retention-class) is set at
`CreateMultipartUpload` (it cannot be set at completion) so the reassembled object's later
install fetch reads the same sensitivity a single upload would.

### 5. Reaper obligation generalizes to "manifest past deadline"

The abandoned-upload reaper drops its owner-must-be-pre-finalize gate: any manifest with
`deadline < now()` is swept, deleting only prefix objects with no committed `artifacts` row
and then the manifest. This closes the one leak chunking introduces — a succeeded Run whose
post-commit chunk cleanup failed leaves a lingering manifest, now reclaimed once the deadline
passes. The per-object no-row predicate (the live-data guard) and the per-owner advisory lock
are unchanged, so a true pre-finalize abandon reaps exactly as before and the committed
reassembled object is never deleted.

### 6. Cap raised to 50 GiB, single-PUT wall preserved

`KDIVE_MAX_UPLOAD_BYTES` default rises 5 GiB → 50 GiB (matching `KDIVE_IMAGE_PRIVATE_MAX_
BYTES`), config-overridable. A single (unchunked) declaration still binds at the 5 GiB
single-PUT ceiling; the 50 GiB cap governs a chunked total. Each chunk is ≤5 GiB, every
non-final chunk ≥5 MiB (the `UploadPartCopy` part-size floor), and `sum(chunks)` must equal
the declared size. `effective_config` keeps its 1 MiB cap and may not be chunked.

## Consequences

- Real-S3 deployments ingest external build artifacts up to 50 GiB; the ≤5 GiB single-PUT
  lane is byte-for-byte unchanged.
- Four object-store multipart primitives are added (`create_multipart_upload`,
  `upload_part_copy`, `complete_multipart_upload`, `abort_multipart_upload`); reassembly is
  server-side copy, so `complete_build` stays synchronous and no artifact bytes transit the
  server.
- Chunked-artifact integrity is per-chunk SHA-256; the whole-object SHA-256 is advisory until
  a download re-derives it — a deliberate, bounded trust point alongside ADR-0048's
  `build_id`.
- The reaper's sweep obligation widens from "pre-finalize owner" to "manifest past deadline,"
  closing the post-commit chunk-cleanup leak with no change to the no-row safety predicate.
- A chunked artifact briefly occupies ~2× storage between reassembly and chunk cleanup,
  bounded by the upload TTL / reaper deadline.
- CI (MinIO, higher single-PUT limit) cannot reproduce the real-S3 single-PUT rejection that
  motivates the feature; that one end-to-end assertion is operator-run, mirroring ADR-0048
  §7's checksum-enforcement verification item.

## Considered & rejected

- **Native S3 multipart upload as the transport.** Parts stream into the final object with
  no reassembly copy and no transient 2× storage. Rejected: it reopens the abandoned-upload
  storage leak in a worse form — an in-progress MPU is invisible to `ListObjectsV2`, forcing
  a new `ListMultipartUploads`/`AbortMultipartUpload` reaper path; it needs persisted
  `upload_id` session state and a new agent round-trip (`complete_upload`) to report part
  ETags; and it diverges from the single-PUT path already verified against MinIO. The
  transient-copy cost it saves does not outweigh reintroducing the leak hazard ADR-0048 §6
  worked to close.
- **Server-side whole-object re-hash at finalize.** Download the reassembled object and
  recompute its SHA-256 to keep a whole-object integrity check. Rejected: it violates
  ADR-0048's no-download finalize contract and re-pulls up to 50 GiB through the server; the
  per-chunk pins already bind every byte, and a whole-object re-derivation stays deferred to
  a plane that downloads the artifact anyway.
- **Composite-checksum verification.** Persist and verify the expected S3 composite checksum
  of the reassembled object. Rejected: brittle — the composite value depends on exact part
  boundaries and S3/MinIO implementation details, and it proves nothing the per-chunk pins do
  not already prove.
- **Rootfs chunking in the same change.** Extend chunking to the System-owned rootfs upload.
  Deferred: it touches the provisioning plane's manifest lifecycle for no #112 benefit
  (#112 is about `vmlinux`/debuginfo). The reaper generalization is owner-agnostic, so a
  later rootfs follow-up inherits it.
- **Keeping the 5 GiB cap and adding a separate chunked cap knob.** Two config values
  (`KDIVE_MAX_UPLOAD_BYTES` + a new chunked-total setting). Rejected for surface simplicity:
  one cap with the single-PUT wall enforced structurally (a no-chunks declaration ≤5 GiB) is
  enough and matches the existing 50 GiB image ceiling.
