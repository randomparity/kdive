# 0683 — Scale authority readiness with its provision intent lifetime

## Status

Accepted (2026-09-26)

## Context

ADR-0680 added a TCG-scaled first-boot readiness wait to regular provision and
explicitly left the authority lane's console window unscaled. The authority lane
starts a 15-minute provision intent before domain preparation but captures a
separate 900-second console window when boot begins. Scaling only the latter
would exceed the configured intent lifetime.

## Decision

Resolve ADR-0341's multiplier from the authority manifest's accelerator once at
composition. Scale both the console window and the authority provision intent
lifetime by that factor. Keep external boot's default console window unchanged.
Reject a configured base console window longer than the 15-minute base intent
lifetime at composition. This compares nominal budgets: work before console
preparation consumes part of the intent lifetime.

Admit `tcg` in the operator-supplied authority manifest and its Ansible
deployment validator, while retaining `kvm` as the default and rejecting
unknown accelerator values. The operator approved this additive contract
change on 2026-09-26.

## Consequences

KVM retains the current budgets. A default TCG authority configuration has a
9000-second console window and a 9000-second intent lifetime. An operator
configuration with a larger boot window fails at assembly with an actionable
error. The intent deadline continues to gate retries at operation entry; this
decision does not move its clock start or promise full remaining console time.

## Considered & rejected

- **Scale only the console window.** verified: the intent is created before
  provisioning and remains fixed at 15 minutes in
  `src/kdive/providers/local_libvirt/system_authority.py`; its nominal budget
  would be shorter than the TCG console window.
- **Reject all TCG configurations.** judgment: it leaves the issue's requested
  emulated-guest readiness behavior unavailable.
- **Move the intent clock start to console preparation.** judgment: that changes
  retained-intent and retry semantics beyond this issue.
