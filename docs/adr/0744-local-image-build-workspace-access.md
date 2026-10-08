# 0744 — Local image build workspace access

## Status

Accepted (2026-10-07)

## Context

Issue #3150 requires worker-created local image builds to boot customization
through the operator-owned session libvirt daemon. Existing temporary directories
inherit the provider group but become mode `2701`. POSIX group permission selection
prevents members of that group from using the `other` execute bit. After granting
group traversal, a mode `0644` disk still denies the operator write access. On an
enforcing host, the build tree's `var_lib_t` label independently denies QEMU access.
Controlled task-owned probes reproduced each boundary and started and destroyed a
real KVM domain after all three were satisfied. This is access evidence, not an
image publication proof.

## Decision

Extend the existing local build handoff, retaining its system-libvirt behavior:

- Before customization boot, add group execute to the owned build directories
  and group read to their regular files. Add group write only to the staged
  boot disk. Preserve ownership, group, existing bits, and the existing system
  daemon's other-execute/other-read access. Grant no directory group write or
  other write in this handoff.
- Reject symlinks, nonregular files and foreign-owned nodes in the temporary
  handoff tree before widening permissions. Use no-follow file descriptors for
  the chmod operation; refuse a changed node rather than modifying its target.
- Host provisioning creates the exact default `/var/lib/kdive/build/images`
  directory under the existing shared-directory contract (operator owner,
  provider group, mode `2770`). This permits builds by successive worker slots.
  It does not recursively change existing build contents or grant access to
  another group's files.
- Add only that image-build subtree to the existing `svirt_image_t` provisioning
  list, retaining SELinux enforcement and the existing restorecon behavior.
  An operator-selected custom workspace requires the same group ancestry and
  confinement prerequisites; no runtime privileged repair is introduced.

The shared provider group already permits cooperating workers and the operator
to operate provider data. This extends that existing access to the active build
disk. It does not create isolation between members of the group.

## Considered & rejected

Do not add world write, run the worker as root, disable SELinux, execute a new
privileged helper, or change daemon ownership. Merely adding other execute fails
for the actual shared-group caller. Merely adding group traversal leaves disk
write and confinement failures. Replacing the whole build pipeline is unnecessary.

## Consequences

The operator can mutate the active customization disk through the existing group.
The published local qcow2 retains that group-write bit after atomic rename; the
worker must finish validation and publication before treating it as registered.
This is the same cooperating-provider trust boundary, not immutable local storage.
Baseline/staging cleanup (#3084), authority publication (#3152), and native POWER
qualification (#2818) remain separate. The operator explicitly approved this permission contract before implementation.
