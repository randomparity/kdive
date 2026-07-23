# ADR 0438 — Rootfs consumer: transport-strip, streaming fetch, and qcow2 format check

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0437](0437-transport-encoding-canonical-object-model.md) (the transport-encoding
  model, the per-owner `accepts_encoding` capability, the manifest persistence, and the shared
  `strip_gzip_to_writer` decode utility this consumes), [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md)
  (the #743 rootfs install/fetch path this extends), [ADR-0228](0228-local-staged-path-catalog-source.md) (the
  connectionless provider-fetch pattern — a sync callable that opens its own short-lived connection
  per call — this reuses to read the manifest).
- **Part of:** epic #1508 (transparent transport-encoding for agent-uploaded objects); this is
  Sub 2 (#1510) — the rootfs consumer. Remote parity + agent-surface docs are Sub 3 (#1511).

## Context

ADR-0437 (Sub 1) landed the generic model: an agent declares a transport `encoding: "gzip"` plus an
`uncompressed_size` on an upload, the declaration validator enforces it, the `upload_manifests` JSONB
persists it, systems' `accepts_encoding=True`, and a shared streaming `strip_gzip_to_writer` gunzips
a ranged-read store object into a writer, bounded (gzip-bomb guard) and hash-verified against the
signed *compressed* sha256. Sub 1 wired **no consumer**.

The rootfs install path (ADR-0434, #743) stages a System-owned uploaded qcow2 by buffering the whole
object into memory and writing it verbatim to the staged base (`rootfs_upload_fetch.py`,
`get_artifact(key, None).data`). Two gaps remain:

1. **No transport strip.** A `gzip(qcow2)` upload is staged verbatim as an invalid qcow2 and fails
   late at `qemu-img`/boot. So the epic's headline case — a rootfs whose canonical qcow2 exceeds the
   5 GiB single-PUT ceiling but whose gzip is a single PUT ≤5 GiB — cannot be consumed.
2. **No format validation.** The uploaded rootfs is checksum-verified only; a wrong-format object
   (whatever it decodes to) fails late at `qemu-img` with a confusing error rather than a clear one
   at staging. (Build uploads are already format-validated — `build_artifacts/validation.py`.)

This ADR wires the rootfs consumer that closes both gaps for the local-libvirt lane.

## Decision

### 1. Where the encoding comes from: the DB manifest, read inside the fetch

The `encoding`/`uncompressed_size` for an uploaded rootfs live only in the `upload_manifests` JSONB
(ADR-0437). The presigned PUT stamps **only** `sensitivity`/`retention-class` into object metadata
(the agent is instructed to send *only* the signed header set), so `head().content_encoding` is
**not** a channel for the declared encoding — the manifest is the single source of truth.

The provider provision seam is connectionless (it runs off the event loop in `asyncio.to_thread`
and owns no async pool). The established pattern for a provider fetch that needs DB-backed data is
**ADR-0228's catalog fetch**: `rootfs_catalog_fetch_from_env` opens its own short-lived *sync*
`psycopg` connection per call. The rootfs upload fetch adopts the same pattern: its
`from_env` wrapper opens a short-lived sync connection, reads the systems manifest's `rootfs` entry
for `(encoding, uncompressed_size)`, and passes them to the pure `fetch_uploaded_rootfs(...)`
function as arguments. The pure function takes no connection and stays unit-testable with an
in-memory store.

This **revises ADR-0434's "the upload fetch needs no DB connection"**: the integrity anchor
(checksum) still rides the object's `head()`, but the *transport encoding* is a manifest fact, so
the fetch now reads the manifest exactly as the sibling catalog fetch already does.

`RootfsUploadContext` (`materialize.py`) is left unchanged — carrying `encoding` on it would be a
redundant second copy of a value the fetch already resolves from the manifest, and the provider
never reads it. (The issue text suggested carrying it on the context populated by the admission
path; that path only *enqueues* the provision job and cannot reach the provider-built context
without abusing the shared `SystemPayload` or a cross-provider port change. Reading it in the fetch,
where it is consumed, is the smaller and pattern-consistent choice.)

**Manifest-absent fallback.** The systems manifest is deleted only after a successful provision
(`_commit_uploaded_rootfs`); during the fetch it is present. If it has nonetheless been reaped
(deadline passed while the job queued — the object is normally reaped with it), the fetch falls back
to **identity** (today's verbatim behavior). A gzip whose manifest was reaped then fails closed at
the qcow2 magic check with a clear message rather than a `qemu-img` error.

### 2. Streaming strip-decompress on the gzip path

`UploadObjectStore` (the fetch's narrow store Protocol) gains `get_range(key, *, start, length)` so
it satisfies `transport_encoding.RangedReadStore` (`ObjectStore` already implements `get_range`).

When the resolved encoding is `gzip`, the whole-object-buffering fetch is **replaced** by a
streaming strip-decompress: `strip_gzip_to_writer` gunzips the ranged-read compressed object into the
`.partial` staging file (never buffering the multi-GiB canonical object in RAM), hashes the
*compressed* bytes against `head.checksum_sha256`, and fails closed if decompressed output exceeds
the declared `uncompressed_size` (gzip bomb). The staged file is `os.replace`d into place only after
a clean decode **and** a passing magic check, so `dest` is only ever a valid, verified base.

The **identity** path is behaviorally unchanged: buffer the object, verify its sha256 against the
stored checksum, and atomically stage it — plus the new magic check below.

### 3. qcow2 magic check, scoped to the upload path

After staging (both the gzip and identity paths), the fetch verifies the canonical base begins with
the qcow2 magic `QFI\xfb` (bytes `51 46 49 fb`) before returning it to back the overlay. A non-qcow2
canonical object is rejected with a clear `CONFIGURATION_ERROR` naming the format problem, not a late
`qemu-img` failure.

**Scope (resolving the epic's open question): the upload path only, not the catalog path.** The
check lives in the upload fetch, so it validates only agent-uploaded bases. Catalog images resolve
through `catalog_fetch` and are pre-vetted at registration (ADR-0228), so they are not
re-magic-checked. Placing the check in the shared overlay creation (`storage.py`) would wrongly
cover catalog bases too; keeping it in the upload fetch scopes it correctly.

### 4. Declaration-time cap

The generic cap fail-fast and the systems cap (50 GiB) already live in Sub 1's shared validator
(`_SYSTEM_UNCOMPRESSED_CAP`, aligned with the `KDIVE_MAX_UPLOAD_BYTES` per-artifact ceiling). Sub 2
adds no cap change — the mechanism and value are already correct.

## Consequences

- The epic's headline case works on local-libvirt: an agent uploads a `gzip(qcow2)` (canonical
  >5 GiB, compressed ≤5 GiB single PUT), and it is streamed-decompressed, magic-validated, and boots.
- Decompression never buffers the canonical object; a gzip bomb is rejected, not expanded (both
  guaranteed by the reused, tested Sub 1 utility).
- Every uploaded rootfs — gzip or identity — is now qcow2-format-validated at staging; a wrong-format
  object fails with a clear message. Catalog bases are unaffected.
- Identity uploads are byte-for-byte unchanged except for the added magic check.
- The upload fetch now opens a short-lived sync DB connection per call (like the catalog fetch),
  revising ADR-0434's connectionless note. No schema, no migration, no MCP/RBAC surface change.
- The `encoding`/`uncompressed_size` fields stay **unadvertised** in the agent-facing upload tool
  schema until Sub 3 (#1511); this ADR only makes systems' `accepts_encoding` functional.

## Considered & rejected

- **Carry `encoding` on `RootfsUploadContext`, populated at admission.** Rejected: the admission
  `_provision_defined_locked` path only enqueues the provision job; the provider builds the context
  later in a worker thread. Bridging admission→provider needs either the shared `SystemPayload`
  (used by breakglass/reconciler/control — semantically wrong to carry rootfs encoding) or a
  new cross-provider `provision()` port parameter (a 5-implementation ripple for data that lives in
  the manifest). Reading the manifest in the fetch, where it is consumed, is smaller and mirrors the
  existing catalog fetch.
- **Read `encoding` from object metadata (`head().content_encoding`).** Rejected: the presigned PUT
  signs only `sensitivity`/`retention-class` and the agent sends only the signed header set, so the
  object carries no `content-encoding`; the manifest is the only source.
- **Magic check in `storage.py` overlay creation.** Rejected: it would validate catalog bases too,
  which are already vetted, and would couple a rootfs-format concern to the generic overlay step.
- **Stream the identity path too.** Deferred: the identity path keeps today's buffered stage (the
  issue requires it unchanged); a streaming identity stage is a separable optimization, not this gap.
