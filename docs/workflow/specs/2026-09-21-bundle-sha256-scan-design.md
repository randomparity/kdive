# Fold bundle SHA-256 into the external-boot scan

Issue [#2570](https://github.com/randomparity/kdive/issues/2570). This is a full-spec,
M-sized change; the fixed 250-line denominator comes from the frozen campaign assessment.

## Problem and scope

External-boot validation currently reads a kernel object in three full passes: preflight,
archive scan, then `_digest_object` for `bundle_sha256`. The digest value is a persisted field
of `external-boot-evidence-v1`, so its bytes and every other evidence field must remain equal
to the current result.

Change only `validation.py`, its direct reader and external-artifact tests, and the measurement
proof record. During the existing archive scan, observe successful store ranges at a monotonic
object-offset seam. After archive parsing, drain any unobserved suffix through that same observer
before obtaining the digest. The scanner may seek or receive overlapping ranges; only the next
contiguous bytes advance the digest. The normal scan therefore supplies the bundle digest without
the standalone kernel `_digest_object` pass.

Do not change the evidence schema or values, security limits, semaphore, other validation paths,
preflight ownership, or chunk-size policy. POWER remeasurement is not an acceptance gate.

## Design

`_RangedReader` receives an optional internal range observer and invokes it only after checking
that a store response fits the requested range. `_ObjectDigest` owns the SHA-256 state and a
monotonic `offset`. For a range beginning before that offset it ignores already-accounted bytes;
for a range beginning after it, it waits. It advances only over the contiguous suffix beginning at
the offset. Its `drain()` requests the first still-unhashed offset through the recorded object
size, preserving the existing short/empty-object failure and range-bound behavior.

`_external_boot_evidence` creates that digest collector and injects its observer into
`_scan_external_boot_archive`. It retains the existing build-ID comparison and optional-initrd
digest ordering, then drains the collector at the former kernel `_digest_object` position and
writes its prefixed value as `bundle_sha256`. `_digest_object` remains for initrd and other
callers; its kernel-bundle call is removed. No caller-visible scan result or evidence structure
changes.

## Success

- For each stored kernel object bounded by its recorded size, the produced `bundle_sha256` equals
  the former full-object SHA-256 and the complete evidence document remains field-for-field equal.
- A gzip tar with bytes after its end marker contributes those bytes to the bundle digest.
- Overlapping/backward scan reads do not double-count; a skipped interval is filled by drain.
- A normal kernel finalization performs no standalone third kernel digest pass; request evidence
  shows fewer kernel ranges than the retained three-pass baseline.
- Empty, short, over-serving, and store-failure range behavior remains a categorized failure;
  a drain-only kernel fault cannot preempt the build-ID or initrd fault that previously preceded
  the kernel digest.

## Global Constraints

Python 3.14, managed with uv. No new dependencies, public contracts, schema changes, or ADRs.
Use the existing `_RANGE_CHUNK_BYTES` and `_build_failure` behavior. The measurement record states
the changed pass/request accounting without claiming a new live measurement.

## Failure model

- **Actors and deployments:** authenticated build finalization against the configured object store;
  focused tests use deterministic in-memory ranged stores.
- **Invariants and assets:** persisted evidence compatibility, complete raw-object digest coverage,
  bounded range responses, and the external-boot security limits.
- **Accepted failure classes:** a short/empty range and a store exception fail validation rather
  than yield evidence; an out-of-order read can be refetched by drain because correctness takes
  precedence over avoiding a request on that unsupported reader sequence.
- **Covered elsewhere:** preflight limits remain `_preflight_external_boot_archive`; admission
  concurrency remains `complete_build.py`; evidence-schema evolution belongs to the ADR-0583 line.

## Validation

- `tests/build_artifacts/test_validation_reader.py`: add direct monotonic-observer coverage for
  backward/overlapping reads, suffix drain, short completion, and empty/over-serving/store drain
  faults; run its focused file.
- `tests/providers/local_libvirt/test_validate_external_artifacts.py`: compare a padded archive's
  complete evidence against the retained legacy construction, assert fewer kernel range calls,
  and prove a drain-only store fault does not preempt the prior build-ID or initrd checks; run the
  focused file.
- `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md`: verify
  truthful pass/request wording with `just docs-links`; prose has no narrower executable consumer.
- Final candidate: `just lint`, `just type`, `just test`, then `just ci` before push.
