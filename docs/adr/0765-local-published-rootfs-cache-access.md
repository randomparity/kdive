# 0765 — Local published rootfs cache access

## Status

Accepted (2026-10-08)

## Context

The four local `images.publish` functional cells required by #3150 include
provisioning and booting the published image. After the workspace correction in
[ADR-0764](0764-local-image-build-workspace-access.md), a native Fedora 44 run
built, customized, validated and published the image. Provisioning then failed:
its worker could not create `/var/lib/kdive/rootfs-cache` beneath the root-owned
provider state directory. The complete cell remained failed.

The local catalog fetch fixes this cache outside the provider's allowed staging
roots. It downloads an S3-backed registered public image, verifies the digest and
publishes a digest-named file. QEMU overlays retain that path as their backing
file. The cache therefore outlives an individual System and is not temporary
build storage. Host preparation currently omits it.

## Decision

Extend the existing host preparation:

- Add exactly `/var/lib/kdive/rootfs-cache` to the shared-directory list. The
  existing task creates or converges this root to the configured operator owner,
  provider group and mode `2770`.
- Add that exact subtree to the existing `svirt_image_t` list. Keep SELinux
  enforcing and use the existing plain `restorecon`, without `-F`.
- Preserve the cache path, allowed staging roots, digest keys, download checks,
  file modes, worker umask, daemon ownership and cache lifetime. Do not add a
  runtime permission repair or privileged helper.

Directory write permits cooperating provider-group members to replace entries,
including files they do not own. This is an explicit extension of the existing
trusted-provider-group model. It does not isolate hostile peers or make cache
contents immutable. Existing cache-hit trust and concurrent publication behavior
remain unchanged; this decision introduces no stronger integrity guarantee.

The installed worker's existing `0022` umask produces readable `0644` cache
files. This decision does not change that umask or recursively repair files on
hosts with a restrictive override. Such hosts must preserve the necessary
operator/hypervisor read access through their existing host policy.

## Existing installations

There is no path migration or automatic cache deletion. Before converging a
populated legacy cache, quiesce Systems depending on its backing files and
inspect the exact root and affected entries. The role changes ownership and
mode only on the named root. Its existing label task covers the named subtree
without resetting customizable security categories.

A legacy entry with insufficient access needs inspection and repair by its
owner or administrator while quiesced. Repair only the named entry's necessary
group/read access and preserve unrelated bits, ownership and contents. Do not
recursively chmod or chown the tree, or delete and redownload a base that may
still back a System. Symlinked or otherwise unexpected legacy roots require
operator reconciliation before preparation; they are not a migration target.

## Considered & rejected

- Moving the cache beneath `build/images` avoids another top-level provider
  directory but mixes persistent backing files with build outputs. A child
  still needs shared-directory policy; relocation also leaves old backing paths
  to preserve until their Systems drain. Keeping the existing path is smaller.
- Moving it beneath the allowed rootfs staging root breaks the intentional
  separation between cached objects and staged-path candidates.
- A manual directory fix leaves clean host preparation broken. Widening the
  provider parent, changing worker or daemon identity, or disabling confinement
  is unnecessary.

## Consequences

Existing cache references remain valid, and clean preparation supplies the
write/traversal and confinement prerequisites for the fixed worker accounts.
Acceptance still requires all four unchanged publication cells to build, boot,
authenticate and release resources on candidate-matched native stacks. A
successful image-build job alone is not a passing cell.

The original workspace rule remains ADR-0764. Baseline/staging cleanup (#3084),
authority publication (#3152), native POWER qualification (#2818), and separately
observed Enterprise Linux image/tool prerequisites are not resolved here.
