# Design — check guest free space before writing the uploaded vmlinux (#3125)

## Scope

Both paths that write a DWARF `vmlinux` into a guest read the guest filesystem's free space
through the libguestfs handle that writes the file, and refuse before the upload when it does
not fit. The fixed-worker `_stage_vmlinux` and the authority `GuestDebuginfoFile.stage` (#3130)
share `guest_free_space.require_free_space_for_vmlinux`. #3131 writes no guest vmlinux on main.
Not in scope: an upload size cap (`validation.py` is untouched), the boot step row (#3135).

### Failure model

- Actors: the install worker and the authority activation, both host-side, on a System overlay.
- Assets: a guest that still boots, keeps room for kdump and logs, and a clear refusal.
- Accepted: another writer fills the guest between the check and the upload (a stopped
  System's overlay is not written by anyone else); the existing upload error path reports it.
- Untrusted: `statvfs` comes from guest filesystem metadata, so negative, non-integer,
  oversized, `bavail > blocks`, or zero/over-1 MiB `frsize` values are refused as untrusted.
- Covered elsewhere: nothing else bounds the upload size (excluded above).

## Decision

- Free space is `bavail * frsize` of the release debuginfo directory, read after the directory
  exists and after any stale staging file is removed, before the upload. Directory creation
  precedes the read; no file bytes do.
- Required: `vmlinux size + 512 MiB`. Boundary: exactly that proceeds; one block less refuses.
- Headroom evidence: measured 6.12.60 DWARF4 unstripped builds are 510,404,448 B (x86_64) and
  425,486,808 B (ppc64le). 512 MiB is one x86_64-sized file, which covers the kdump initramfs
  rebuild plus crash dumps and logs on the guests the live proofs used. It is a module
  constant, reversible by editing it.
- The authority path keeps the prior file beside the live name until recovery, so the full
  size is needed there too; the check does not credit the prior file.
- Failure: `CategorizedError(INSTALL_FAILURE)` naming the directory, vmlinux bytes, free bytes,
  headroom and the fix (enlarge the System root disk by N bytes or build a smaller debuginfo).
  Details carry the same numbers. Untrusted statistics fail closed with the same category.
- Size source: the fixed worker stats the host file; the authority path fstats the projection
  artifact descriptor. `statvfs` joins `InactiveGuest` and the guarded wrapper.

## Verification

`test_debuginfo_file.py` (authority) and `test_stage_vmlinux_free_space.py` (fixed worker):
enough space proceeds, exact boundary proceeds, one block short refuses with no upload, and
hostile statistics refuse with no upload. `test_external_boot.py` runs the full activation with
the fd-derived size. A real-image `statvfs` call is not exercised here; see the PR body.
