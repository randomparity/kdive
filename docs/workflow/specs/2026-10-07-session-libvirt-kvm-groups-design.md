# Selected session libvirt KVM startup (#2877)

Approved startup-owner and migration design (2026-10-07).
Scope q2877-3f8a69e5; M250 full-spec. Existing approved exclusions remain unchanged.

## Problem and evidence

A user manager started before operator group grants retains its old supplementary groups.
At 95e28bcac, the real rendered unit reproduced native x86_64 capabilities with only `qemu`
on Ubuntu26.04.1, while a fresh operator process opened KVM and created a VM successfully.
The current `sg` selects the socket group but does not add KVM supplementary membership.
Nested `sg` and `newgrp` probes did not retain both groups. Selecting only KVM prevented
initializing the configured worker-group socket. Login membership is not daemon evidence.

## Decision and ownership

Apply [ADR0737](../../adr/0737-system-manager-starts-operator-session-libvirt.md).
The role installs only `kdive-libvirtd-live.service` at system scope, running as the same
operator UID/primary group with the already granted KVM and worker socket groups.
Keep the executable, configuration, PID/socket tuple, session URI and runtime directories.
No root-running libvirtd, new privilege grant, helper, device chmod or provider policy.
No operator user-manager restart. The authority daemon startup owner remains unchanged.

The installed service's maintenance command changes from `systemctl --user` to privileged
`systemctl`; this management-interface change received explicit checkpoint approval.
Existing unprivileged direct-start recovery stays available for its established no-sudo
callers. It may create an unmanaged selected daemon when the installed unit is unavailable;
provisioning refuses takeover until the operator drains and stops that daemon. System ownership
is the normal installed startup path, not a claim of exclusive daemon launch authority.

## Migration and errors

Before replacing the old user unit, inspect its state under the configured operator manager.
An active legacy unit fails with instructions to drain its Systems and stop only that named
unit, then rerun. Provisioning does not stop active domains or the user manager automatically.
Disable/remove only the inactive role-owned legacy unit file; retain shared user directories.
Existing protected tuple validation and stale PID/socket cleanup remain the sole cleanup owner.
A live selected daemon must match the new system unit's MainPID; refuse unmanaged or conflicting
processes with the same drain/stop/rerun instruction. Do not infer ownership from process name.
Fresh setup enables/starts the system unit. Repeat setup retains its live matching daemon.
A reboot initializes current groups independently of the operator user manager.

## Readiness and compatibility

Query capabilities through the exact selected operator URI as the operator and, when local
mutation is enabled, through the exact authority URI as its configured identity. Reuse the
production `parse_capabilities_arch` and `parse_guest_arches` parsers and SUPPORTED_ARCHES.
Require the native guest entry to advertise KVM on this KVM-required provisioning role.
Malformed/unknown/empty capabilities or connection errors fail before System mint, naming
which endpoint failed and the installed service to inspect. Foreign guest TCG remains allowed;
general discovery, deliberate TCG provider configurations, and accelerator/XML checks do not change.

### Failure model

- Actors/deployments: administrator-managed Ubuntu26.04 live_vm_host, operator user manager
  already running or freshly started; configured fixed workers and optional local authority.
- Invariants/assets: same operator UID, socket authority, targeted cleanup, real native KVM
  evidence, foreign TCG capability, and unrelated user services remain intact.
- Accepted failures: unavailable host KVM, invalid endpoint XML, active legacy/unmanaged daemon,
  or contradictory tuple evidence stop provisioning with an actionable diagnosis; none qualifies
  as a completed native proof or authorizes metadata/permission repairs.
- Covered elsewhere: unrelated service/device policy is operator-owned and excluded;
  native POWER qualification remains #2818; discovered carrier defects retain existing owners.

## Validation

Exercise the source-rendered old and candidate units on the same disposable nested-KVM host
with the operator manager started before group grants. Preserve the red evidence; require the
candidate endpoint KVM advertisement and unchanged manager PID plus an unrelated sentinel unit.
Test fresh install, inactive legacy migration, active-legacy refusal, unmanaged-live refusal,
repeat setup and reboot. Verify actual configured endpoint selection and identity in role tests,
including native KVM plus foreign TCG, native TCG failure and malformed XML failure.
Run focused provisioning tests, affected Ansible harness, lint/type/shell/Ansible gates.
Mandatory prepush owns full justci; CI remains distinct from installed evidence.
At exact deployed candidate, verify both native endpoints, mint a registered-root System with
accel=kvm, and run tests/live_vm/test_installed_local_authority.py normal operations.
Report existing-owner prerequisite/carrier failures separately; no partial result is full green.
Root retains disposable-host reset authority. No POWER or final qualification claim follows.
