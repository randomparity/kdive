# ADR-0603: Inspect remote storage by device identity

## Status

Accepted

## Context

Remote module preparation must prove that the System root, source, and scratch volumes have no
unexpected domain attachment before it changes storage. Libvirt XML can name the same host object
through a volume, direct path, symlink, hard link, or bind mount. Lexical path normalization and
libvirt's managed-volume lookup do not unify all of those names, while treating every unmanaged
disk as a conflict would block unrelated tenants.

## Decision

The remote-libvirt attachment inspector consumes a provider-local `RemoteDeviceIdentityPort`.
Given one validated absolute host path, the port returns an opaque `(device, inode)` identity after
following host filesystem aliases. Inputs are bounded to 4,096 encoded bytes. Device and inode are
non-negative bounded integers; paths never appear in the result or in errors.

The inspector resolves both the protected managed-volume paths and every direct file/block source
through this port. Equal identities conflict regardless of their lexical names; distinct identities
remain unrelated. An absent or invalid identity fails closed as a conflict. A host lookup operation
that cannot complete raises `INFRASTRUCTURE_FAILURE` with a redacted message.

The port is intentionally provider-local. The server-preparation adapter that can execute at the
remote host supplies it; the generic lifecycle provider interface does not expose host filesystem
details.

## Consequences

Attachment inspection detects symlink, hard-link, and bind-mount aliases without disclosing remote
paths. Every path-bearing disk source must be observable from the remote host during preparation;
an unobservable source blocks the destructive operation. Existing unrelated unmanaged disks remain
allowed when their identity is established and differs from the protected identities.

## Considered & rejected

- **Continue lexical normalization plus libvirt volume lookup.** verified: the #2167 confirming
  review demonstrated that libvirt's directory-pool lookup retains exact path naming and does not
  unify symlink, hard-link, or bind-mount aliases.
- **Reject every unmanaged file or block disk.** judgment: this would turn unrelated tenant disks
  into false conflicts instead of proving whether they share protected storage.
- **Put host filesystem identity on the generic lifecycle port.** judgment: local filesystem
  identity is an implementation detail of remote-libvirt preparation and does not belong in every
  provider runtime.
