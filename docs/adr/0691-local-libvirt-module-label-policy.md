# 0691 Bind local-libvirt module labels before publication

## Status

Accepted (2026-09-27)

## Context

ADR-0583 requires the installed module-tree identity to include filesystem labels. Local-libvirt
currently derives it from an unlabeled archive. A Fedora guest relabels the tree on first boot,
so recovery sees a different identity even when the contents are unchanged. ADR-0584 binds the
intended target identity before the authority permits provider mutation; moving that binding to
after publication would change the fencing contract.

## Decision

For local-libvirt, preparation reads the inactive guest's SELinux configuration and file-context
specifications through the existing guarded guest capability. When SELinux is enabled, the provider
uses host libselinux's raw lookup to resolve a context for each final
`/lib/modules/<release>/...` path and
computes the installed manifest with those `security.selinux` values. A missing policy, unmatched
path, or failed evaluation stops preparation. With SELinux disabled or absent, the existing
unlabeled manifest remains the target.

Activation evaluates the same guest policy, writes each label while populating the staging tree,
and observes the staged tree before publication. The staged manifest must match the immutable
target identity recorded at preparation. A policy change or failure between the two phases is a
conflict, not a reason to update the identity. Recovery continues to compare every xattr exactly;
the source capture and restore format is unchanged. The local guest-tree adapter replays captured
SELinux xattrs on regular files, directories, and symlinks through libguestfs's string-and-length
binding so recovery can restore the recorded source identity. Host libselinux is a worker
prerequisite and is provisioned on local-libvirt and live-VM hosts.

`ExternalBootMaterialization.installed_module_tree` remains the canonical archive-derived
unlabeled manifest used to revalidate materialized bytes. For local-libvirt SELinux guests,
preparation binds the separately computed, labeled installed manifest in the recovery point.
Other providers retain their existing behavior.

## Consequences

The guest policy is evaluated on the host without running guest binaries, including when guest
and host architectures differ. The provider must bound and validate copied policy files before
passing them to libselinux. Hosts without libselinux cannot prepare SELinux guests and fail with
an actionable error. A guest policy change during an activation requires a new preparation;
recovery never silently accepts a newly assigned label.

## Considered & rejected

- **Bind target identity after publication.** judgment: this would change ADR-0584's authority
  request and takeover ordering for a provider-specific labeling problem.
- **Run guest `setfiles` on the staging path.** verified: libguestfs `setfiles` accepts the actual
  path and no alternate-root argument, so rules for the final release path can differ; see the
  libguestfs `setfiles` API documentation.
- **Run `setfiles -r` through libguestfs `command`.** verified: libguestfs `command` runs guest
  binaries and requires a compatible processor architecture; the supported x86_64 host to
  ppc64le guest path cannot rely on it (libguestfs `command` documentation).
