# 0730 — System manager starts operator session libvirt

## Status

Accepted (2026-10-07)

## Context

Issue #2877 reproduces a lingering operator user manager without newly granted KVM groups.
The daemon started through its user unit advertises only native TCG even when a fresh operator
process can create a KVM VM. The dedicated socket still needs its existing worker group.
Restarting the whole user manager would terminate unrelated services and is excluded.

## Decision

For live_vm_host, install the selected session libvirtd as a system unit running with User=
the configured operator and its existing primary and supplementary provider groups. Preserve the
session URI, daemon UID, runtime tuple, socket permissions and existing direct-start fallback.
The latter supports existing no-sudo callers; it does not confer system-unit management authority.

Refuse active legacy or unmanaged daemons before takeover, naming the operator's drain/targeted
stop/rerun action. Remove only the inactive legacy user unit. Reuse protected tuple reconciliation.
Verify native KVM through the actual selected endpoints, using production capability parsing;
foreign guest TCG and the ordinary provider discovery contract remain unchanged.

## Consequences

Installed service administration uses privileged systemctl rather than systemctl --user.
No new operator elevation permission is installed. Other user services and their manager remain
running. Existing hosts need one explicit targeted legacy stop after draining Systems.
A direct-start fallback can leave an unmanaged selected daemon; provisioning refuses automatic
adoption and gives the same bounded recovery instruction. Native POWER still needs native proof.

## Considered & rejected

- Keep only fail-fast readiness and manual fresh-login launch. judgment: smaller, but repeated
  automatic user-unit starts retain stale groups, so normal setup still cannot repair the cause.
- Restart the operator user manager. verified: systemd user services share that manager;
  the approved #2877 exclusions prohibit restarting unrelated services.
- Nested sg/newgrp. verified: on Ubuntu26.04.1 with the manager started before grants, transient
  systemd-run probes reported only the selected primary group plus old supplementary membership;
  neither retained both KVM and socket groups. Source-derived baseline capabilities lacked KVM.
- Broaden device/socket access. judgment: conflicts with the approved permission boundary.
