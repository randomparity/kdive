# Bound guest regular-file reads (#2853)

## Problem and scope

The session downloads a guest file in full before comparing its size. A controlled
fixture stored 1 MiB against a declared three bytes before raising the existing error.
The session remains the owner of bounded transfer for module capture and SELinux policy.
Scope and empty exclusions are frozen in issue #2853, token q2853-7e09ac31.

## Design

Use the existing libguestfs `pread(path, count, offset) -> bytes` API in chunks of at
most 1 MiB. Copy only the declared size into the operation-owned temporary file.
Continue after short nonempty reads; reject EOF before the declared size and a response
larger than requested before writing it. Probe one byte at the declared end; a nonempty
result is a size-change error and is never stored. Flush and retain the final exact-size
check, rewind, then yield the file. Negative sizes fail before opening temporary storage.
The existing context manager closes the file after success, transfer error or caller error.

The ceiling is the caller's declared byte size per read, enforced during transfer, plus
one unstored probe byte. A size mismatch raises ValueError before yielding; callers fail
the current capture or policy read and may retry only through their existing operation
path after obtaining fresh metadata. No new timing deadline is introduced.

ADR-0704 records this transport choice. Public interfaces and persisted formats stay
unchanged. Tests using the concrete guest seam gain `pread`; artifact downloads retain
their existing distinct method.

## Success

- Exact-size, empty and multi-chunk binary files are returned byte-for-byte.
- Stale small metadata cannot cause the temporary file to exceed its declared size.
- Truncation, overrun, transfer failure and caller failure close temporary storage.
- Module capture and SELinux policy consumers retain their existing contracts.
- A real x86 libguestfs appliance proves the selected binding on the candidate source.

## Failure model

1. **Actors and deployments:** local-libvirt workers and installed authority on supported
   x86_64 and ppc64le Linux hosts; guest file bytes and stale metadata are untrusted.
2. **Invariants and assets at stake:** host temporary storage cannot exceed the supplied
   size; each requested data payload is at most 1 MiB and transfer memory is independent of total
   file size; failures never yield a partial file.
3. **Accepted failure classes:** equal-length concurrent content changes and growth after
   the EOF probe are not a coherent-snapshot guarantee; existing inactive guest/session
   ownership governs writers. Host ENOSPC and binding I/O failures propagate and close the
   temporary file; the declared size remains a caller-selected limit, not a global quota.
4. **Covered elsewhere:** source eligibility and total capture budgets belong to recovery
   capture and SELinux policy callers; unrelated artifact downloads retain their owner.

## Validation

Focused unit cases exercise boundaries, short reads, errors and cleanup using real
TemporaryFile storage; existing recovery and policy tests cover direct consumers.
The live arm creates only a disposable ext4 appliance, uploads controlled binary files,
then drives the concrete session read seam with real GuestFS.pread and verifies actual
stored bytes and closure. Native ppc64le repetition may be tracked separately under the
operator's explicit verification direction; x86 evidence is required before delivery.
