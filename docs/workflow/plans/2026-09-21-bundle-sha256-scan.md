# Fold bundle SHA-256 into the external-boot scan

Goal: remove the standalone kernel digest pass while preserving byte-identical
`external-boot-evidence-v1` output. `validation.py` owns ranged object reads and archive
validation; its tests own reader and finalization contracts; the proof record owns historical
measurement interpretation. Python 3.14 and uv remain in use; no dependency, schema, limit,
preflight, semaphore, or chunk-size changes are allowed.

Expected implementation size: 250–310 changed lines (M) — one internal digest collector, narrow
reader wiring, focused contracts, and one measurement-record correction.

## File map

- `src/kdive/build_artifacts/validation.py` owns range observation, drain, and evidence assembly.
- `tests/build_artifacts/test_validation_reader.py` owns direct cursor/range fault contracts.
- `tests/providers/local_libvirt/test_validate_external_artifacts.py` owns full evidence and
  validation request-accounting contracts.
- `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md` owns the
  published description of the validation passes and measured request total.

## Task 1 — Prove the monotonic digest seam

**Files:** modify `tests/build_artifacts/test_validation_reader.py`.

**Interfaces:** borrows `_RangedReader(store, key, size, range_observer=...)`, whose existing
three positional arguments remain valid. Later code supplies `_ObjectDigest.observe` and
`_ObjectDigest.drain`. Drain uses the reader's existing bounded range-fetch rule before each
observation, accepts nonempty short responses by continuing at the next offset, and fails on an
empty or over-serving response.

**Verification:** Mode: focused-test. A new test makes sequential reads, seeks backward and
forward, drains, then compares the prefixed digest to `hashlib.sha256(blob)`; it also asserts the
drain fetches only the missing interval. A short nonempty drain response completes on a later
fetch. Empty, over-serving, and store-exception drain responses raise the existing categorized
failure before a digest value is available. Before implementation the observer/collector names
are absent; after:
`just test-verbose tests/build_artifacts/test_validation_reader.py`, exit 0.

1. Add focused tests first: complete monotonic coverage despite seeks; short-then-complete drain;
   and empty, over-serving, and store-error drain faults. Run the file and retain the expected red
   import/attribute failure.
2. Add an internal collector with `observe(start: int, data: bytes) -> None`,
   `drain() -> None`, and `value() -> str`; its only state is digest and contiguous offset.
3. Extend `_RangedReader` with an optional observer. Invoke it only after the existing oversized
   response check, passing the store-range start and returned bytes. Run the focused file green.

Acceptance: buffering and seek semantics remain unchanged, while hashing advances only the
monotonic byte prefix and a missing tail fails rather than producing a partial digest.

## Task 2 — Consume the scan-side digest in evidence

**Files:** modify `src/kdive/build_artifacts/validation.py` and
`tests/providers/local_libvirt/test_validate_external_artifacts.py`.

**Interfaces:** `_scan_external_boot_archive(..., range_observer: Callable[[int, bytes], None] |
None = None)` keeps its mapping return; `_external_boot_evidence` owns construction, drain, and
`bundle_sha256` assembly.
`_digest_object(store, key, size_bytes)` remains unchanged for initrd.

**Verification:** Mode: focused-test. Build a valid gzip kernel tar with trailing raw padding,
compute the retained legacy evidence using `_scan_external_boot_archive` plus `_digest_object`,
then assert the new full document equals it. Count kernel ranges in equivalent stores and assert
the new flow has fewer calls. Add a store whose trailing-only request fails while a mismatched
vmlinux build ID or an initrd fault is also present; each pre-existing earlier failure wins. The
test is red if the old standalone kernel digest remains. Green:
`just test-verbose tests/providers/local_libvirt/test_validate_external_artifacts.py`, exit 0.

1. Add the padded-object equality, range-reduction, and drain-fault precedence tests before source
   changes, including an initrd so the retained initrd digest path is distinguished from the
   removed kernel path.
2. Inject the collector observer into the scan reader. After the existing build-ID and optional
   initrd paths complete, drain and use its value for `bundle_sha256`; remove only the kernel
   `_digest_object` invocation.
3. Run both focused test files, then `just lint`, `just type`, and `just test-changed`; all exit 0.

Acceptance: no evidence field is added, removed, or changed; trailing bytes affect the bundle
digest; normal validation removes the third kernel pass without suppressing error behavior.

## Task 3 — Correct the proof record and verify the candidate

**Files:** modify the measurement proof record.

**Interfaces:** no executable interface; it cites the validation pass topology established by Task
2.

**Verification:** Mode: task-test-not-applicable. Measurement prose has no independent executable
consumer; `just docs-links` checks its links and final `just ci` covers documentation guards.

1. Replace the three-pass description with preflight plus scan-side hashing, and state that the
   former total request count includes the removed standalone digest pass and is no longer current.
   Do not invent a replacement live measurement.
2. Re-read the diff for schema/limit/preflight/semaphore scope, run `just docs-links`, then the
   full final `just ci` with blocking stream redirection.

Acceptance: published measurement language is truthful about the removed pass and does not claim
unperformed POWER or live remeasurement.
