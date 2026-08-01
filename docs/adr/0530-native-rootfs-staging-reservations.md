# 0530 — Reserve uploaded-rootfs staging blocks with native fallocate

## Status

Accepted (2026-08-01)

## Context

ADR-0450 refuses a single uploaded-rootfs stage when its required bytes plus the staging floor
exceed the filesystem's available bytes. The check is advisory: fetch locks are keyed per base, so
two different bases can both observe the same free bytes and then jointly fill the volume.

The partial's `flock` guard already owns a writable descriptor for the stage lifetime, but the
identity and gzip writers reopen the path with `"wb"`. That `O_TRUNC` would discard a reservation.
`os.posix_fallocate` is not suitable as the allocator because glibc may emulate it by writing zeros
when the filesystem lacks native support, turning a rejected 50 GiB stage into a 50 GiB write.
Gzip adds a second constraint: its declared `uncompressed_size` is an upper bound, so unused
reserved blocks must be released before publish.

## Decision

After the advisory precheck creates and locks the unique partial, staging calls the native Linux
`fallocate(2)` operation on the guard descriptor for the budgeted length. The call uses mode zero:
the partial's logical length becomes the reservation length, and allocation is atomic with the
filesystem's accounting. `ENOSPC` and quota exhaustion fail the stage before download with an
attributable `INFRASTRUCTURE_FAILURE`. Two different bases remain parallel; the filesystem, not a
KDIVE-wide lock, decides which reservation wins.

The stagers receive the guard descriptor and write through a duplicated descriptor without
`O_TRUNC`. The guard remains the owner of the `flock` and the allocation lifetime; the duplicate
only gives each writer its own closing file object. Identity consumes its exact reservation. Gzip
records the decoder's actual output count and truncates the partial to that count after successful
decode and transport-checksum verification, before the shared qcow2 and durability gates. A failed
stage unlinks the partial and thereby releases every reserved block.

The native call is made directly rather than through `posix_fallocate`, with an explicit `ctypes`
prototype whose `off_t` arguments are 64-bit on KDIVE's supported x86_64 and ppc64le hosts.
`ENOSYS` and `EOPNOTSUPP` mean native reservation is unavailable and log one warning before
continuing under ADR-0450's advisory precheck and mid-write `ENOSPC` handling. Other allocation
errors are staging failures. KDIVE never falls back to `posix_fallocate`, so an unsupported
filesystem cannot trigger glibc's zero-writing emulation.

No database state records reservations. They are inode allocations owned by a unique partial and
survive exactly as long as that partial, including process death until an existing partial sweep
unlinks the orphan. No migration or operator setting is added.

## Consequences

- Concurrent different-base stages cannot both reserve more blocks than the filesystem or quota
  can provide; one wins and the other fails before reading its object.
- The existing one-GiB advisory floor remains a single-stager admission policy, not a globally
  reserved floor. Other volume writers can still consume free space after a reservation succeeds.
- Identity verifies the streamed byte count against the exact HEAD size as well as checking the
  digest, so a changed or faulty object-store response cannot publish a zero-padded reservation.
- Gzip temporarily reserves its declared upper bound and releases the unused tail before publish.
  An overstated bound can therefore lose a race for capacity even when the eventual image is small.
- Filesystems without native allocation support keep the pre-0530 behavior, with a warning that
  the concurrency guarantee is unavailable on that staging volume.
- A killed worker can leave a fully reserved orphan partial until the existing opportunistic or
  reclaim sweep removes it. The reservation is path-visible and follows the same cleanup contract
  as every other staging partial.

## Considered & rejected

- **Use `os.posix_fallocate`.** Rejected because libc may emulate it with writes on unsupported
  filesystems, creating the volume pressure this change is meant to avoid.
- **Serialize all staging on a volume-wide lock.** Rejected because it collapses the per-base
  parallelism required by ADR-0441 and still cannot serialize guest-overlay or external writers.
- **Keep separate `"wb"` writer handles.** Rejected because their `O_TRUNC` releases the
  reservation before the first object byte is written.
- **Subtract sibling partial sizes from free space.** Rejected by ADR-0450: `f_bavail` already
  accounts for written bytes, while partial length says nothing reliable about future allocation.
- **Keep only the advisory precheck.** Rejected because it leaves issue #1546's deterministic
  concurrent overcommit unchanged.
