# Local image build workspace access

Status: Approved permission rule.

## Authority and outcome

Issue #3150 under #2803 requires local images.publish to build, customize,
validate, publish, boot, and clean up on the supported operator-session host.
Frozen scope: issue comment 6049719556, token q3150-c0f0f510. The user approved
original scope/exclusions on 2026-10-07; the operator explicitly approved this reviewed permission rule. ADR-0744 records
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

Implement ADR-0744 in the existing local handoff helper and its sole `_run_boot`
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
