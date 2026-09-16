# Raise `_RANGE_CHUNK_BYTES` to cut external-boot validation store requests (#2569)

## Problem

`_RANGE_CHUNK_BYTES` (4 MiB, `src/kdive/build_artifacts/validation.py:57`) sets the `get_range`
granularity of every external-boot validation pass. #2495 measured 1484 store requests for one
2 GiB finalization: ~20 s at the 13.53 ms/request seen on a ppc64le loopback store.

## Scope

Raise it to **8 MiB** and comment it with the resident footprint that implies and why that holds
under `_EXTERNAL_BUILD_VALIDATION_SLOTS = asyncio.Semaphore(1)`
(`src/kdive/services/runs/complete_build.py:47`). No logic changes. Out of scope: every #2569
charter exclusion, and the same-named constant in `kdive/artifacts/uploads/transport_encoding.py`.

8 MiB, not 16 MiB: the constant sizes one `_RangedReader` read-ahead buffer *and* five
decompressed-side loops that step by it, one (`_magic_offsets`) holding two at once. Footprint is
linear in the value; request reduction is sublinear — halving takes 50% of the reachable saving,
16 MiB only 25 points more — and 16 MiB would collide numerically with three unrelated constants.

### Failure model

**Actors and deployments** — an authenticated CONTRIBUTOR finalizing an external build on the
single-host deployment, server-side under that semaphore. Bundle bytes are attacker-influenced; no
anonymous caller reaches this code.

**Invariants and assets at stake** — evidence value identity (`bundle_sha256`, `vmlinuz_sha256`,
member counts, byte totals, every `external-boot-evidence-v1` field); the resident-memory bound on
an untrusted-input path; the four `_EXTERNAL_BOOT_*` limits, whose enforcement must not move.

**Accepted failure classes**
- Peak resident bytes per finalization double. Accepted: `Semaphore(1)` admits one validation at a
  time, so the worst case is ~8 MiB of reader buffer plus ~16 MiB transient, against the 512 MiB
  member size this code already permits.
- `tests/build_artifacts/test_validation_reader.py` sizes blobs from the constant, so each case
  allocates 8 MiB not 4 MiB. Accepted: bounded, and measured under Validation.
- The #2318 proof record's request counts go stale. Accepted: stated in #2569, and re-measuring on
  POWER is not a criterion (`docs/debt/0015-ppc64le-finalization-measurement-unrun.md`).

**Covered elsewhere** — further request reduction: #2570, #2571. Semaphore capacity: its own issue.

## Success

1. `_RANGE_CHUNK_BYTES` is `8 * 1024 * 1024`.
2. For one fixed bundle, the complete validation result is equal across chunk sizes below, at, and
   above the object size, including one dividing it exactly.
3. That bundle's `get_range` count strictly falls when the constant rises.
4. The constant's comment names the footprint and its `Semaphore(1)` dependency.

## Validation

- **Evidence is chunk-size-independent, and a larger chunk issues fewer requests.** focused-test —
  `tests/providers/local_libvirt/test_validate_external_artifacts.py`: validate one fixed
  multi-chunk bundle under several monkeypatched `_RANGE_CHUNK_BYTES` values (one dividing the
  object exactly, one exceeding it), asserting every result equal and the count strictly falling.
  Red under a fault that makes a result chunk-dependent.
- **The constant is 8 MiB and the reader window contracts hold there.** focused-test —
  `tests/build_artifacts/test_validation_reader.py`, unedited: its cases derive the window from the
  constant, so they re-prove the boundary at whatever value it holds.
- **The comment records the footprint.** task-test-not-applicable — prose; no consumer parses it.
