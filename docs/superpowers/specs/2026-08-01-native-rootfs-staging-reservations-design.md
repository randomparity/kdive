# Spec: Native uploaded-rootfs staging reservations (#1546)

- Issue: [#1546](https://github.com/randomparity/kdive/issues/1546)
- ADR: [ADR-0530](../../adr/0530-native-rootfs-staging-reservations.md)
- Status: Design accepted

## Requirement

The uploaded-rootfs free-space check must become an allocation-backed reservation for filesystems
that support native allocation. If two different-base stagers race and their combined requirements
exceed the staging volume's free blocks, exactly one stages successfully and the other fails with a
capacity diagnosis. The implementation must not route through glibc's potentially zero-writing
`posix_fallocate` emulation, must settle descriptor ownership and gzip over-reservation, and must
preserve per-base parallelism.

The frozen scope excludes a global staging lock, sibling-partial accounting, configuration,
database persistence, and changes outside local-libvirt rootfs staging plus focused tests and docs.
Migration 0096 is assigned but is not needed because reservations are kernel-owned inode state.

## Chosen mechanism

`stage_uploaded_rootfs` retains ADR-0450's HEAD, directory creation, and advisory precheck ordering.
Inside `_flocked_partial`, after the exclusive liveness lock is established, it asks a private
native-allocation seam to call Linux `fallocate(2)` with mode zero for `_StagingBudget.required`.
This call is atomic with filesystem allocation accounting. The first concurrent caller reserves
its blocks; a second caller that cannot reserve its full requirement receives `ENOSPC` or `EDQUOT`
before it opens an object stream.

The allocation seam calls the process libc's native `fallocate` symbol directly with `ctypes` and
reads `errno` on `-1`. It does not call `os.posix_fallocate`. Missing native support (`ENOSYS`,
`EOPNOTSUPP`, or the mode-zero interface rejected with `EINVAL`) returns an explicit degraded
result. Staging logs that the volume has only ADR-0450's advisory protection and continues. Other
errors flow through `_staging_fault`; capacity errors retain `INFRASTRUCTURE_FAILURE` and add
reservation-specific scalar details so the losing System and destination are attributable.

This is preferable to a volume-wide KDIVE lock, which would serialize unrelated bases without
covering guest overlays or external writers. It is also preferable to `posix_fallocate`, whose
fallback can perform the entire zero write before reporting success or failure.

## Descriptor and reservation ownership

The descriptor created by `_flocked_partial` owns both the liveness `flock` and the reservation.
Each stager accepts that descriptor, duplicates it, seeks the duplicate to byte zero, and wraps the
duplicate as its writer. `fdopen` does not reopen the pathname and does not apply `O_TRUNC`, so the
preallocated extents remain. Closing the duplicate cannot release the BSD `flock`, which remains
owned by the guard's open file description through verify and durable publish.

Mode-zero native fallocate makes the partial's logical length equal the budget. This is safe for
identity because the budget is the exact stored-object length and checksum verification rejects an
early or corrupt stream. Gzip's budget is only an upper bound. `strip_gzip_to_writer` already
returns the actual decompressed byte count; `_stage_gzip` returns that count to its caller, which
`ftruncate`s the guard descriptor to the actual count immediately after successful decode and
transport checksum verification. The shared qcow2 magic check and `_durable_replace` therefore see
only verified canonical bytes, never the zero-filled reservation tail.

Any exception before publish reaches the existing `finally` discard. Unlinking the partial releases
its blocks. Process death leaves a visible reserved orphan under the existing partial name; the
flock-based opportunistic and reclaim sweeps remain its owners and release the reservation when
they unlink it.

## Error and degrade contract

- `ENOSPC` and `EDQUOT` from native allocation are attributable reservation failures. The error
  identifies the System, destination, requested bytes, budget source, and operating-system errno,
  and tells the operator to free capacity and re-issue provisioning.
- `ENOSYS`, `EOPNOTSUPP`, and interface-level `EINVAL` log one warning and stage under the existing
  advisory precheck. A later write-side `ENOSPC` retains `_staging_fault`'s current behavior.
- KDIVE never tries `os.posix_fallocate` after native allocation reports unsupported. This is the
  executable guard against glibc's emulated zero-writing path.
- Other allocation errors are ordinary attributable staging faults and leave no partial.
- Reservation success does not promise the one-GiB floor against unrelated writers. The floor is
  still checked at admission, while the reservation guarantees only the base's budgeted blocks.

## Concurrency proof

A deterministic test drives two different destinations on the same emulated volume through a
barrier in the private native-allocation seam. The fake allocator has capacity for either full
requirement but not both and performs its debit under a lock. Both calls must reach allocation
before either returns; exactly one returns success, the other raises `ENOSPC`, exactly one object
stream is consumed, and the failure details identify its System and destination. Because the two
stagers use different partials and no KDIVE-wide lock, reaching the barrier together also proves
per-base staging remains parallel.

A separate degrade test makes the native seam report `EOPNOTSUPP`, replaces
`os.posix_fallocate` with a sentinel that fails if called, and proves the stage still succeeds with
the degrade warning. This directly exercises the native-unavailable/emulated-POSIX hazard rather
than merely asserting a helper return value.

Focused writer tests additionally prove that an identity stage does not truncate a preallocation,
gzip shrinks an over-reservation to the decoder's actual length before the qcow2 gate, allocation
failure leaves no partial, and the existing flock remains held after the writer duplicate closes.

## Threat model

### Boundaries and actors

No new entry point is added. The existing authenticated tenant controls uploaded bytes and the
gzip `uncompressed_size` declaration; the local worker controls the staging path and descriptor;
the host filesystem controls allocation and errno results. The change widens the existing tenant
effect from progressive writes to an up-front reservation bounded by the same 50 GiB declaration
cap already enforced before provisioning.

### Controls

- The existing upload declaration validator and canonical-size cap bound every reservation.
- The destination remains derived from the investigation-scoped content address, not tenant path
  input, and each attempt still uses an exclusive UUID partial.
- Kernel allocation accounting arbitrates concurrent reservations; no user-space observation is
  treated as authoritative.
- Unsupported native allocation degrades without invoking an emulating API, and every failure path
  discards the partial through the existing `finally`.
- Errors expose paths and scalar capacity facts already present in local-libvirt job failures; they
  expose no uploaded bytes or credentials.

### Out of scope

This change does not reserve the staging floor against guest overlays, other KDIVE writers, or
operators; does not prevent an authorized tenant from consuming its allowed staging capacity; and
does not shorten the existing interval before a killed worker's orphan is swept. Those limits are
unchanged and do not invalidate the issue's two-stager acceptance criterion.

## Acceptance criteria

1. A deterministic concurrent test with capacity for one of two bases produces exactly one staged
   base and one attributable `INFRASTRUCTURE_FAILURE`; both callers reach allocation concurrently.
2. Native-allocation unavailability stages successfully under the advisory check, logs the degrade,
   and never calls `os.posix_fallocate`.
3. Identity and gzip writers write through the guarded inode without pathname `O_TRUNC`; gzip
   releases the unused reservation tail before format verification and publish.
4. Allocation and writer failures leave no partial or published base; successful stages preserve
   the existing checksum, qcow2, fsync, marker, and sibling-publish gates.
5. Different bases remain parallel. No schema, migration, dependency, setting, or MCP contract is
   added.

## Verification

The focused proof runs `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_upload_fetch.py -q`.
Repository verification runs `just ci`. The concurrency mutation replaces the native reservation
with an unconditional success: both stages then consume their streams and the exactly-one assertion
fails. The degrade mutation calls `os.posix_fallocate`: the sentinel test fails immediately.
