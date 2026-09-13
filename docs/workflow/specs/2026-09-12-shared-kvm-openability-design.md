# Shared KVM openability design

## Problem

The Python KVM diagnostic tests URI-selected presence or permissions while the shared build budget
has a separate `O_RDWR` probe. A static KVM node can be present and world-writable yet fail open
with ENODEV, so the diagnostic and appliance budgets disagree on an emulated host.

## Scope

Replace the URI-selected filesystem decision with one config-snapshot `KDIVE_KVM_NODE` openability
probe in `guest_arch_accel`; inject `open` and `close` seams for tests; use it from the diagnostic,
local appliance deadline, shared build timeout, and live-stack fixture. Delete only the duplicated
shared timeout probe. Update the external-env catalogue and create ADR-0648.

Do not change per-tool budget callers, shell checks, or the provider boundary. Do not claim that an
open node proves a libvirt KVM domain for each architecture.

## Success

- `KDIVE_KVM_NODE` is snapshot-resolved on each default probe invocation; empty or missing values
  select `/dev/kvm`.
- For local and remote URI strings alike, `O_RDWR` success attempts one close and returns true;
  an `OSError` from opening or closing returns false.
- Native acceleration and both host-appliance budget paths share that result; their existing
  injected seams and timeout/scaler behavior remain intact.
- ADR-0648 records the narrow supersession and residual per-architecture limit.

## Failure model

- **Actors and deployments:** local KDIVE workers and CI unit tests; no new remote input path.
- **Invariants and assets at stake:** doctor acceleration output and timeout scaling must not claim
  KVM when the configured node cannot open; successful probes must not leak descriptors.
- **Accepted failure classes:** openability does not establish `KVM_CREATE_VM` or a KVM domain for
  every architecture, because this issue has no approved capability-discovery mechanism.
- **Covered elsewhere:** shell-tier readiness convergence is owned outside #2410; provider-import
  direction is enforced by `tests/providers/test_provider_boundaries.py`.

## Validation

- `tests/diagnostics/test_guest_arch_accel.py`: injected open/close, URI independence, override,
  empty fallback, open/close OSError, and native TCG reporting.
- `tests/providers/shared/test_build_timeouts.py`: default shared budget consumes the shared probe
  and preserves unscaled/scaled branches.
- `tests/providers/test_provider_boundaries.py`: shared module remains free of local-libvirt
  implementation imports.
- `just lint`, `just type`, focused pytest paths, `just records`, and documentation guards pass.
