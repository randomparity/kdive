# Issue 894 Modules Prefix Design

## Problem

External `kernel` tarballs rooted at `./boot/vmlinuz` and `./lib/modules/...` pass
`runs.complete_build`, but local-libvirt install cannot determine the module release from
the repacked modules archive. `repack_modules_subtree()` selects members using normalized
names, then writes the original `TarInfo.name`; `_read_release()` strips only `/`, so
`./lib/modules/...` does not match `lib/modules/`.

## Contract

Every tar member shape accepted by combined-kernel validation for `lib/modules/...` must
remain accepted by local-libvirt install. The install path must treat `./lib/modules/...`,
`/lib/modules/...`, and `lib/modules/...` equivalently when determining the release and
when writing a modules-only archive.

Path traversal members remain rejected by the existing `..` segment check.

## Implementation

Normalize modules member names at the repack boundary before writing them to the
modules-only tar. This gives the guest writer a canonical `lib/modules/...` archive and
keeps extraction paths predictable.

## Testing

Add a regression test in `tests/providers/local_libvirt/test_install.py` that builds a
combined tar with `./boot/vmlinuz` and `./lib/modules/<release>/...`, repacks it, verifies
the repacked archive contains canonical `lib/modules/...` names, and verifies
`_RealGuestKernelWriter._read_release()` returns the release.
