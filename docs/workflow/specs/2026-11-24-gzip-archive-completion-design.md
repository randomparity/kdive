# Complete bounded gzip archive validation

Decision: [ADR-0869](../../adr/0869-bounded-gzip-archive-completion.md).

## Problem

Tar framing ends before gzip framing. `_GuardedTarReader` stops at the first
zero header and its later read loop returns EOF without consuming the underlying
gzip stream. Actual full-scanner probes accept missing trailers, corrupt CRC and
a late invalid deflate member. The public prefix check rejects small malformed
uploads, but accepts those beyond its scan window; a reduced test cap reproduces
that arm without allocating a 128 MiB fixture. Materialization uses the full scanner too.

## Scope and design

Add one completion method to the existing guarded reader, which already owns raw
framing accounting. After successful tar iteration, call it instead of the ineffective
wrapper drain. It reads the existing gzip source to EOF in chunks bounded by
`_DECODE_CHUNK_BYTES` and the remaining `_EXTERNAL_BOOT_ARCHIVE_MAX_BYTES` allowance
plus one detection byte. Before a read, reject an already-negative allowance; never
issue a nonpositive/unlimited read. Charge returned decoded tail to the same raw
counter as declared padded tar members and terminators; reject if the limit is crossed.
Keep logical regular-file bytes and published manifest counts/digests unchanged.

Catch actual `zlib.error` alongside existing gzip/tar errors at the scanner boundary.
Use the existing complete-readable-gzip build failure; operational categorized store
failures propagate unchanged. Call completion only after the successful tar walk,
not in `finally`, so earlier unsafe-member and framing failures are not masked.
Subsequent missing-boot/module and release checks retain their current order.

Keep valid concatenated gzip streams, zero padding and decoded trailing data within
the raw budget; completion validates compression, not a new trailing-content policy.
Keep the prefix cap, object-version pinning, whole-compressed-object digest and
per-member/ELF/module validation unchanged. Document complete gzip/raw-budget behavior
in existing artifact contract notes and upload guide; no new tool/schema field or counter.

## Failure model

- Actors/deployments: uploaded kernel-bundle producers, finalization and local
  materializer callers, for x86_64 and ppc64le. Proof uses real parser/decompressor
  and fake storage/libvirt boundaries on Linux x86_64, not guest/hardware qualification.
- Assets: structural archive completeness, CPU/memory bounds, exact-version evidence,
  failure categories and prevention of publication/provider mutation after rejection.
- Accepted classes: valid trailing decoded data remains ignored after tar EOF, subject
  to the existing total raw limit. No new CPU deadline or total-process RSS guarantee.
- Covered elsewhere: object-store transport retry/version guarantees, source-object
  digest verification, authority fencing and actual guest boot retain their own checks.

## Success

1. Both architecture arms reject missing/CRC/ISIZE/late-deflate corruption through
   the full scanner and public prefix-cap arm; valid/padded/concatenated controls pass.
2. Completion charges framing and trailing output, accepts an exact raw limit,
   rejects the next byte, bounds each read and never allocates a whole trailing stream.
3. Existing malformed-member errors retain priority; categorized source failures
   preserve identity. Valid evidence values and source-version/digest behavior stay equal.
4. Existing local materializer validation still rejects mismatched evidence before
   publication; shared scanner closure and all applied SQL remain protected.
5. Tests, lint, whole types, documentation/record gates and hooks pass; independent
   bounded design/code/security reviews close before a local source commit.

## Validation

Use actual `_combined_kernel_tar`, architecture-specific boot helpers, `_FakeStore`
and `_validate_kernel_blob` in the existing artifact test module. The old-source
scanner and prefix-cap corrupt cases must fail with DID NOT RAISE, with valid controls.
The default small-public arm remains a baseline rejection control. Reader tests
exercise bounded raw accounting with small limits and a recording source; deliberate
removed-finish and removed-budget faults must fail, with byte-exact restoration.
Run existing reader, artifact, upload-contract, materializer and kernel-bundle suites;
include the existing version-pinning/digest/semantic-primary cases. Inspect the exact
contract resource output rather than snapshotting prose. No model judge is needed:
this is deterministic validation, not new agent decision guidance. Final campaign CI
and whole-branch reviews remain separate requirements.
