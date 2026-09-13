# 0648 — Shared KVM openability probe for appliance budgets and acceleration diagnostics

## Status

Accepted (2026-09-12)

- **Issue:** #2410
- **Supersedes:** the shared-KVM-probe and host-appliance-budget portions of ADR-0352 and ADR-0637

## Context

ADR-0352 made `kvm_probe_for_uri` choose `os.access(..., R_OK | W_OK)` only for
`qemu:///session` and `os.path.exists` for every other URI. ADR-0637 then added a second,
independent `O_RDWR` probe for the shared libguestfs budget. The results disagree for a present
node that the worker cannot open, and the diagnostic can report native KVM while both appliance
budgets use emulation.

Permission bits are not openability. On Fedora ppc64le with KVM unavailable, udev can publish a
world-writable static `/dev/kvm`; `os.access` succeeds and `os.open(..., O_RDWR)` returns ENODEV.
Opening is also allowed to autoload KVM. The probe does not issue `KVM_CREATE_VM` and closes a
successful descriptor immediately.

## Decision

The provider-agnostic diagnostics contribution owns one worker-host KVM probe. Its node comes
from `KDIVE_KVM_NODE` in `config.env_snapshot()`, with empty and absent values falling back to
`/dev/kvm`. For each invocation it opens that path `O_RDWR`, attempts to close a successful
descriptor once, and returns false for an `OSError` from either operation. URI text no longer
changes the filesystem test.

`guest_arch_accel`, `host_appliance_multiplier`, the shared `appliance_budget_s`, and live-stack
test defaults use that probe. `providers/shared/build_timeouts.py` deletes its private probe but
retains the scaler and all current callers. The environment catalogue documents the unified Python
consumer set.

This does not decide whether libvirt advertises a KVM domain for a particular host architecture.
An open node with no matching KVM domain remains an accepted unresolved failure class until a
separate, evidence-backed per-architecture probe is designed.

## Consequences

- The diagnostic and both host-side appliance budgets make the same openability decision for the
  configured node, including missing, denied, and ENODEV nodes.
- A successful open may autoload KVM and does not prove VM creation or per-architecture KVM-domain
  support.
- `KDIVE_KVM_NODE` remains a catalogued external variable, not a registry Setting; no direct
  `providers/shared` to local-libvirt implementation import is introduced.
- Shell readiness checks retain their current contract and are outside this decision.

## Considered & rejected

- **Keep URI-selected presence or permission checks.** verified: issue #2410 owner evidence
  records `os.access` true and `os.open` ENODEV on a static KVM node, so either check reports KVM
  when the appliance emulates.
- **Keep the private build-timeouts probe.** verified: the existing source has two implementations
  of the same node decision; routing every current Python consumer through one injected seam makes
  their contract testable together.
- **Probe `KVM_CREATE_VM` or libvirt domains here.** judgment: that would change this worker-host
  openability contract into per-architecture capability discovery without the required live
  evidence or approved scope.
