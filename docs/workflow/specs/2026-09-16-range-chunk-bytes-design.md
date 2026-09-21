# External-boot validation range chunks (#2569)

## Problem

The 4 MiB validation chunk incurred 1484 requests in the recorded ppc64le finalization.
Larger chunks reduce sequential store requests without changing evidence.
#2476's scanner cursor fix landed in #2599; equivalence needs proof against that source.

## Scope

Set `build_artifacts/validation.py::_RANGE_CHUNK_BYTES` to 8 MiB and explain its memory cost.
Keep existing ownership and algorithms: ranged reader, digest, discard, module/boot reads,
magic scanning, bounded kernel copies (including gzip/zstd), and preflight decompression.
8 MiB halves large sequential request counts; 16 MiB would double buffers again for less saving.
Annotate both architecture row sets in the dated #2318 measurement record as superseded,
stating 4→8 MiB and expected inverse request scaling without inventing new measurements.
Tests use the existing external-artifact fixtures and ranged-reader contracts.
The approved charter excludes evidence/schema changes, security limits/enforcement,
semaphore changes, mandatory POWER remeasurement, other validation optimization,
#2570/#2571, and upload transport's independently owned constant. No ADR is required.

### Failure model

- **Actors and deployments:** authenticated contributors on server deployments; uploaded
  archives are untrusted. The existing validation semaphore admits one scan per process.
- **Invariants and assets:** complete evidence identity, validation outcomes and existing
  archive/decoded-work/metadata bounds; increased transient memory must be described honestly.
- **Accepted failure classes:** roughly six simultaneous explicit chunk buffers (~48 MiB):
  reader window, retained boot-copy chunk, scanner chunk/data pair, compressed input and
  decoded output. Replacement/copy peaks and native codec workspace add memory; two existing
  64 MiB spools are separate. This is not a total-RSS bound. Serial admission prevents
  concurrent scans multiplying this per-process cost; raising it requires reassessment.
- **Covered elsewhere:** #2570/#2571 own pass elimination; admission control owns concurrency.
  POWER remeasurement is excluded; resolved debt 0015 is historical evidence.
- **Threat model:** no new boundary; tenant-controlled archive/codec bytes still cross the
  validator. Existing member, archive, candidate, decoded-work and metadata limits govern it.
  Authentication belongs to the service caller; changing those controls is excluded.

## Success

1. The production chunk is 8 MiB, with its footprint/admission comment.
2. Complete validation results agree across old/new and scaled chunk sizes, including
   multi-chunk objects, exact boundaries, decoys and supported compression codecs.
3. Sequential request counts fall for objects larger than the larger chunk and plateau
   when a chunk spans the object; no exact halving of whole-finalization totals is promised.
4. Both measured architecture row sets retain their historical values with dated annotations.

## Validation

- **focused-test:** external-artifact tests compare complete results on identical objects
  across chunk sizes, plus old/new production request counts; a chunk-dependent evidence
  fault must fail. Run `just test-verbose tests/providers/local_libvirt/test_validate_external_artifacts.py`.
- **focused-test:** existing reader boundaries plus a production read-ahead assertion;
  restoring 4 MiB must fail the latter. Run `just test-verbose tests/build_artifacts/test_validation_reader.py`.
- **task-test-not-applicable:** footprint explanation and historical annotations are prose
  without executable consumers; review against buffer lifetimes and both original row sets.
  Run `just lint`, `just type`, `just docs-links`, `just docs-paths`, and final `just ci`.
