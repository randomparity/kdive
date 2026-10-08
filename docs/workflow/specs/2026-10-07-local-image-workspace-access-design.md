# Local image build workspace access

Status: Workspace and published-cache permission rules approved.

## Authority and outcome

Issue #3150 under #2803 requires local images.publish to build, customize,
validate, publish, boot, and clean up on the supported operator-session host.
Frozen scope: issue comment 6049719556, token q3150-c0f0f510. The user approved
original scope/exclusions on 2026-10-07; the operator explicitly approved this reviewed permission rule. ADR-0764 records
the exact bounded rule. Baseline/staging directories
(#3084), authority publishing (#3152), and native POWER (#2818) are excluded.

## Verified failure and ownership

`build_workspace` creates a private TemporaryDirectory. `_run_boot` calls
`_grant_hypervisor_traversal` after extracting the baseline, then the existing
customization boot starts QEMU through the configured libvirt connection.
The current helper adds other execute/read only. A member of the inherited
provider group cannot traverse the resulting group-inaccessible directory.
A separate staged-disk write denial remains after traversal is granted, followed
by an independent enforcing-SELinux denial under var_lib_t. Three controlled
probes isolated those boundaries; the final task-owned domain started and was
destroyed after traversal, disk group write and svirt_image_t labeling.

## Behavior

Implement ADR-0764 in the existing local handoff helper and its sole `_run_boot`
caller. Pass the staged disk explicitly so no other file gains group write.
Preserve the existing system-daemon path and remote build behavior. Validate the
entire temporary handoff tree before permission changes; no symlink target or
foreign-owned node may be widened. Keep file descriptor ownership bounded and
close all opened descriptors on success and failure.

Provision the default build/images root through the existing shared-directory
list and add this exact subtree to the existing SELinux image-label list.
Update the owning role tests and host-check documentation. Do not alter the
existing rootfs/install entries or generic shared-workspace helper.

A custom workspace remains operator-configured: it must have appropriate
traversable ancestors, a shared group when the daemon is another UID, and the
correct confinement label. Runtime permission changes apply only within the
current build directory. No new configuration, CLI, privilege or helper exists.

## Verification contract

- Reproduce current group-class denial with the actual fixed worker and operator
  session libvirt connection; retain independent disk-write and SELinux probes.
- Regression tests assert group traversal/read, disk-only group write, unchanged
  unrelated bits/owner/group, no other-write grant, and same-user operation.
- Adversarial tests reject symlink/foreign-owner/nonregular inputs and confirm
  outside targets remain untouched; verify cleanup after errors.
- Test two worker slots can create workspaces under the role-provisioned default
  root. Run the Ansible role harness and confinement checks, preserving enforcing
  mode and all other fixtures.
- Run all four unchanged local images.publish functional cells in
  tests/integration/test_run_tool_cells_live.py (direct/gateway, default/recovery)
  against an exact candidate-matched three-role stack. Each must build, publish,
  boot/authenticate and release its actual resources. The carrier requires fresh
  backing state for each configuration; the campaign root owns reset scheduling.
- A failing downstream baseline/staging cleanup remains #3084 evidence. Missing
  prerequisites or carrier failures remain failed/blocked obligations, never a
  substituted mock pass. Native POWER remains pending.

## Failure model and limits

The permission step is not a transaction: an OS error can leave some owned nodes
with intended additive access before TemporaryDirectory cleanup. It must fail
before boot, retain an actionable error and never widen an outside target.
Provider group members are already cooperating principals; this change does not
promise adversarial race isolation from an actor that can replace the workspace
through its shared parent. No blanket existing-tree migration is permitted.

## Approved published-cache prerequisite extension

The first unchanged native Fedora cell at `3e4c546d` successfully built and
published its image, then failed to provision it because the fixed worker could
not create `/var/lib/kdive/rootfs-cache`. The complete cell remains failed.
Root retained this prerequisite under #3150. The original workspace grant does
not authorize this additional permission surface.

Accepted [ADR-0765](../../adr/0765-local-published-rootfs-cache-access.md) retains
the fixed persistent backing path outside allowed staging roots and adds only
that exact root/subtree to the existing shared-directory and SELinux lists.
No runtime cache code, file mode, umask, digest/cache-hit/concurrency behavior,
daemon identity or cleanup lifetime changes. Explicitly accept that trusted
provider peers with directory write can replace cached entries. There is no
claim of hostile-peer isolation or immutable cache content.

Before applying to a populated legacy cache, quiesce dependent Systems. Repair
only inspected, named legacy paths through their owner/administrator; no
recursive chmod/chown, automatic deletion, relocation or live-base redownload.
The installed worker's existing `0022` umask provides readable cache files;
a restrictive override must retain equivalent read access through host policy.

After design review and explicit approval, extend the two role lists, their
owning tests and operator documentation. Verify exact-root modes/labels,
independent creation by two actual worker UIDs, operator read and outsider
denial, with SELinux enforcing. Preserve staged-path separation and existing
cache behavior tests. Then rerun all four unchanged publication cells on fresh
candidate-matched stacks; retain failed earlier records separately. Final
review must cover the complete expanded branch, not only the list entries.

## Publication provenance oracle

The required native publication cell exposed a carrier mismatch after a successful
build and provisioning: the producer records `os_release.id` and
`os_release.version_id`, while the guest identity comparator consumes `ID` and
`VERSION_ID`. Map these two canonical fields explicitly before comparing the
published provenance, preserving the existing architecture and version checks.
Keep this assertion in the existing image-smoke support module so its owning
ordinary tests do not import a collected live-test module. Missing or mismatched
OS/version/architecture must still fail. This changes no product schema or cell
identity; rerun all four required publication cells after correcting the oracle.
