# 0739 — Share local baseline and install-staging directory access

## Status

Accepted (2026-10-07)

## Context

Issue #3084 reproduces a worker creating baseline and install-staging directories as mode2755
under an existing setgid provider-group root. Another slot and the operator cannot unlink their
contents. Both receive PermissionError through the existing teardown helpers. Regular kernel files
can remain0644: unlink authority comes from the containing directory, not write access to the file.

## Decision

The baseline temporary-directory creator and persistent install-staging System/Run creators
ensure group read, write and search on the directories they create. Preserve other permission
bits and the inherited group; do not change files, service umasks, roots or unrelated directories.
Centralize this three-site rule in the existing local-libvirt storage module. Open the final
component as a directory without following a symlink and operate on that descriptor. A directory
already satisfying the group-access rule is reusable, including one owned by another slot. Only
its owner may add missing group bits; a foreign-owned restrictive directory fails with an
explicit owner/admin repair instruction. No new privileged helper or automatic ownership change.

Older directories need a bounded operator repair: quiesce work for the identified System,
verify its exact baseline/System/Run directories are real directories with the configured
provider group and expected creator, then have that owner or administrator grant g+rwx on those
specific directories. No recursive chmod, wildcard fleet sweep, file-mode change or symlink repair.
The separate optional install scratch path retains its existing policy; it is not a persistent
System artifact reclaimed by this teardown contract.

## Consequences

New artifacts become reclaimable through existing cross-slot and reconciler cleanup. Baseline
atomic publication and existing teardown ordering are unchanged. The configured provider-group
members gain directory mutation authority, consistent with their existing shared overlay/root
access; the operator still owns group membership and protected root provisioning. A restrictive
foreign-owned legacy directory stops a retry until bounded repair. Native POWER proof remains due.

## Considered & rejected

- **Change the worker umask to0002.** judgment: affects every file the service creates, including
  unrelated outputs, instead of the three directory sites responsible for this failure.
- **Run cleanup as root or add a chmod service.** judgment: new privileged authority is unnecessary
  when the creating process owns the new directory and the operator can repair legacy paths.
- **Recursively rewrite rootfs/staging permissions.** judgment: exceeds bounded repair, affects
  unrelated artifacts and obscures the actual creator defect.
- **Make kernel files group-writable.** verified: actual slot1-created0644 files are unremovable
  beneath2755 directories by peers; directory unlink/search permission is the missing authority.
