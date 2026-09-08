# External-build validation range buffering

Issue: [#2317](https://github.com/randomparity/kdive/issues/2317), part of
[#2314](https://github.com/randomparity/kdive/issues/2314).

## Problem

External-build archive validation wraps the version-fenced object store in `_RangedReader`, but
each parser-sized `read()` becomes one `get_range()` request. `gzip` and `tarfile` commonly ask for
10 KiB at a time, so validating an otherwise ordinary archive makes thousands of object-store
requests as artifact size grows. The parent issue measured 117 range calls for a 1 MiB fixture,
including 102 requests for 10 KiB.

The reader is also the boundary where EOF, short-response, oversized-response, and cursor behavior
must remain precise. Optimizing this boundary must not weaken the immutable-version fence in
`_ObservedVersionStore`, archive bounds, checksums, canonical paths, member identity, or failure
classification.

## Scope

Change `_RangedReader` into a bounded, seekable read-ahead reader. It keeps one contiguous byte
window of at most `_RANGE_CHUNK_BYTES` (4 MiB), fetched from the current cursor. A read wholly
inside that window performs no store request. A miss replaces the window with one request of at
least the requested byte count and at most 4 MiB, bounded by recorded object size. A caller request
larger than 4 MiB may fetch that requested size directly; the retained window still never exceeds
4 MiB. This preserves the existing contract that one `read(size)` may return fewer than `size`
bytes when the object store does.

`tell()` reports the logical cursor. `seek(offset, whence)` accepts `SEEK_SET`, `SEEK_CUR`, and
`SEEK_END`, rejects an unknown `whence`, and rejects a negative resulting position. Seeking within
the retained window reuses it; seeking elsewhere invalidates it. Seeking beyond recorded EOF is
allowed, and a subsequent read returns `b""`, matching ordinary seekable binary files.

The reader continues to call the injected store, so `_ObservedVersionStore` remains the sole
identity fence and supplies the exact version id on every underlying request. No dependency or
public response contract changes. Durable finalization, jobs, cancellation, retry/idempotency,
MCP behavior, and representative 103-MB/2-GB ppc64le timing remain owned by #2318/#2319.

## Data flow and failures

Both archive passes construct independent readers. Each pass therefore owns an independent buffer
and cannot accidentally reuse bytes across validation stages. The checksum pass remains a separate
stream and is unchanged.

An empty store response at a non-EOF cursor remains observable as `b""`; callers retain their
existing truncated-input failures. A response larger than the requested fetch bound remains a
`BUILD_FAILURE`. A short non-empty response is buffered exactly as returned and advances the
logical cursor only by bytes delivered to the caller. No speculative request crosses the recorded
object size.

## Threat model

**Existing boundary changed.** Authenticated tenants control uploaded archive bytes and their
declared size; the object store controls range-response length. The design adds no boundary and
widens no actor's reach. `_ObservedVersionStore` continues to replace caller-supplied version ids
with the version recorded by the first `HEAD`.

**Controls.** Recorded size bounds every read-ahead request. The 4 MiB retained-window cap bounds
memory independently of archive size. Oversized responses fail before being exposed. Empty and
short responses cannot synthesize bytes and therefore flow into existing completeness checks.
Archive member count, compressed/decompressed byte caps, canonical-path checks, module topology,
checksums, and build identity are unchanged above the reader.

**Out of scope.** This change does not address a malicious or inconsistent store returning wrong
bytes for a pinned version; checksum and content validation remain the controls for that threat. It
does not alter timing budgets or transport recovery, which #2318 and #2319 own.

## Success

1. Sequential small reads across a valid incompressible external-build archive whose compressed
   size is from 1 MiB through 2 MiB require at most eight kernel range calls for the complete
   `validate_external_artifacts` operation. This count includes the content check, both independent
   archive readers, and the checksum pass. The pre-change implementation makes 117 calls on the
   1,049,341-byte fixture, so the ceiling is fixed before implementation and fails red.
2. Buffer-boundary reads and seeks return the same byte sequence as a seekable in-memory file.
3. EOF, seek-beyond-EOF, invalid seek, `read()` through recorded EOF, caller reads larger than the
   retained window, empty/short/oversized responses, and store exceptions retain explicit, tested
   behavior.
4. Every underlying archive request remains pinned to the immutable version observed by `HEAD`.
5. Existing external-build validation, archive limits, identity checks, and failure categories
   remain green on x86_64 and architecture-neutral code remains valid for ppc64le.

## Validation

Focused unit tests in `tests/build_artifacts/test_validation_reader.py` exercise `_RangedReader`
directly: sequential reads and request count, reads crossing a 4 MiB boundary, a caller read larger
than 4 MiB without retaining more than 4 MiB, `read()` through EOF, all supported seek modes,
in-window seek reuse, out-of-window invalidation, EOF, seek beyond EOF, negative/invalid seek
rejection, short and oversized responses, and unchanged propagation of a store exception.

An integration-style validation regression in
`tests/providers/local_libvirt/test_validate_external_artifacts.py` builds an incompressible
external-boot fixture, runs `validate_external_artifacts`, asserts success and immutable version ids,
and requires no more than eight total kernel range calls, counted across content checking, both
archive readers, and checksumming. The exact focused green commands are recorded in the
implementation plan. Repository proof is `just lint`, `just type`, focused pytest,
`just test-changed`, and `just ci > FILE 2>&1 < /dev/null`.
