# External-boot validation range chunks (#2569)

## Problem

The 4 MiB validation chunk incurred 1484 requests in the recorded ppc64le finalization.
Larger chunks reduce sequential store requests without changing evidence.
#2599 fixed scanner seeks. Corrupt gzip candidates still make decode accounting
chunk-dependent; the approved amendment preserves the existing decoder quantum.

## Scope

Set `validation.py::_RANGE_CHUNK_BYTES` to 8 MiB for store reads and introduce
`_DECODE_CHUNK_BYTES = 4 * 1024 * 1024` for local streaming/decoder calls.
Retain the existing quantum in discard, module/boot reads, magic scanning and bounded
kernel copies, including gzip/zstd. Route prefix decompression through the existing
`_RangedReader`: fetch 8 MiB but deliver 4 MiB to zlib. No ownership/algorithm transition.
8 MiB approximately halves long sequential request counts.
Annotate both architecture row sets in the dated #2318 measurement record as superseded,
stating 4→8 MiB and expected inverse request scaling without inventing new measurements.
The approved charter excludes evidence/schema changes, security limits/enforcement,
semaphore changes, mandatory POWER remeasurement, other validation optimization,
#2570/#2571, and upload transport's independently owned constant. No ADR is required.

### Failure model

- **Actors and deployments:** authenticated contributors on server deployments; untrusted archives.
- **Invariants and assets:** complete evidence identity, validation outcomes and existing
  archive/decoded-work/metadata bounds; increased transient memory must be described honestly.
- **Accepted failure classes:** roughly 28 MiB of explicit simultaneous chunk buffers:
  8 MiB reader plus five 4 MiB chunks: retained boot-copy, scanner chunk/data, compressed input,
  decoded output. Replacement/copy peaks and native codec workspace add memory; two existing
  64 MiB spools are separate. This is not a total-RSS bound. Ordinary uncancelled calls
  serialize; cancelled awaiters can leave overlapping scan threads (ADR-0656, Cancellation).
- **Covered elsewhere:** #2570/#2571 own pass elimination; admission control owns concurrency.
  POWER remeasurement is excluded; resolved debt 0015 is historical evidence.
- **Threat model:** no new boundary; tenant-controlled archive/codec bytes still cross the
  validator. Existing member, archive, candidate, decoded-work and metadata limits govern it.
  Authentication belongs to the service caller; changing those controls is excluded.

## Success

1. Store ranges are 8 MiB; local decode/accounting stays 4 MiB, with footprint commentary.
2. Complete validation results agree across old/new and scaled chunk sizes, including
   multi-chunk objects, exact boundaries, decoys and supported compression codecs.
   Corrupt-after-output decoys preserve rejection under the same aggregate budget.
3. Sequential request counts fall for objects larger than the larger chunk and plateau
   when a chunk spans the object; no exact halving of whole-finalization totals is promised.
4. Both measured architecture row sets retain their historical values with dated annotations.

## Validation

- **focused-test:** external-artifact tests compare complete results and corrupt-decoy
  rejection across store sizes, plus production request counts. Coupled decoder quantum
  must fail. Run `just test-verbose tests/providers/local_libvirt/test_validate_external_artifacts.py`.
- **focused-test:** existing reader boundaries plus a production read-ahead assertion;
  restoring 4 MiB must fail the latter. Run `just test-verbose tests/build_artifacts/test_validation_reader.py`.
- **task-test-not-applicable:** footprint explanation and historical annotations are prose
  without executable consumers; review against buffer lifetimes and both original row sets.
  Run `just lint`, `just type`, `just docs-links`, `just docs-paths`, and final `just ci`.
