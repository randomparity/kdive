# 0685 — Operator power off requests a clean shutdown

## Status

Accepted (2026-09-26)

Amends [ADR-0028](0028-control-plane-power-force-crash.md) §1 for local-libvirt
`power off`; uses [ADR-0679](0679-local-libvirt-clean-power-off.md)'s bounded helper.

## Context

ADR-0028 maps operator `power off` directly to `destroy()`. That discards guest page-cache writes
even when the guest could shut down. Install and boot now request a clean shutdown first; operator
power off remains the hard-stop exception. The operator chose clean shutdown with a bounded hard
fallback for #2801.

The `Controller.power(domain_name, action)` port has no accelerator argument. The looked-up domain
has the authoritative libvirt XML type: `kvm` for KVM and `qemu` for TCG.

The power handler releases its System transaction lock before provider IO. A longer shutdown
wait would let a force-crash job commit `CRASHING` and fire an NMI, only for the waiting OFF job
to destroy the guest during kdump. The crash guard rejects power jobs after the marker; it does
not stop one that passed its precheck earlier.

## Decision

For `PowerAction.OFF`, accept a domain already in `SHUTOFF`, then read the looked-up domain's XML
type and call the shared `power_off` helper with `clean_shutdown_bound_s(accel)`. `kvm` maps to
`kvm`; `qemu` maps to `tcg`. Invalid XML, a missing type, or an unsupported type fails as
`CONTROL_FAILURE` without issuing a stop command for an active domain.

The helper accepts an already-shut-off domain, requests shutdown for a running guest, waits up to
60 seconds on KVM or 60 seconds times the configured TCG multiplier (600 seconds by default),
and then destroys the guest if it remains active. A
paused or crashed domain goes straight to destroy. A shutdown call that blocks inside libvirt can
overrun the bound by that one call, as ADR-0679 documents. Libvirt operation failures retain the
existing `CONTROL_FAILURE` mapping. The `Controller` port and other power actions stay unchanged.

For OFF only, the worker holds a session-scoped advisory lock on the System key from its READY
precheck through the provider call. Its short database transactions commit while the session
lock stays held. A later force-crash marker waits until OFF finishes; an earlier marker makes the
OFF precheck reject the job. Cancellation of the handler does not stop a libvirt call already
running in a thread, so the handler waits for that thread to finish before releasing the lock
and propagating cancellation. The connection's prior autocommit mode is restored on provider
failure or cancellation. Other power actions keep their current route.

## Consequences

- Operator power off can take the full wait when a running guest ignores shutdown.
- A cooperative guest can flush writes before the System stops.
- XML read or validation failure does not turn an unknown accelerator into a hard stop.
- A force-crash marker cannot land during an OFF job's bounded clean-stop wait. A force-crash
  job may wait for that bounded stop before sending its NMI.
- Cancellation waits for the in-flight provider thread to finish before unlocking; it can be
  delayed by the helper's bound and one blocking libvirt shutdown call.

## Considered & rejected

- **Keep a hard stop.** judgment: it preserves the data-loss behavior the operator chose to
  remove for this action.
- **Add an accelerator argument to `Controller.power`.** judgment: it widens the provider port
  and callers when the looked-up domain already carries the execution type.
- **Offer a configurable wait or force action.** judgment: neither is in #2801's approved scope.
- **Leave the OFF provider call outside the System fence.** verified: the power handler releases
  its transaction lock before provider IO (`src/kdive/jobs/handlers/control/control.py`,
  `1a66127f`), and `tests/adversarial/test_provider_state_races.py` permits one pre-marker power
  op; a longer OFF wait can then destroy during kdump after a concurrent crash marker.
