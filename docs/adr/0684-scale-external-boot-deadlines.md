# 0684 — Carry one local external-boot timing snapshot to the authority

## Status

Accepted (2026-09-26)

Amends [ADR-0681](0681-external-boot-clean-power-off.md) on activation and recovery
budgets. Its TCG recovery stop remains hard for the reason below.

## Context

Local external boot opens a 900-second console window by default, while worker authority
calls and server-persisted recovery requests have 300-second deadlines. The window is not
scaled for TCG. The server, worker, and provider-authority host may read separate environment
files; their `KDIVE_LIBVIRT_BOOT_WINDOW_S` and multiplier settings need not agree. The worker
has the System's persisted accelerator, while the authority host reads the owned inactive
domain XML. ADR-0683 scales the separate authority-provision path, not this one.

## Decision

At server admission, snapshot local external-boot timing from the persisted `System.accel` and
the server's configured boot window and ADR-0341 multiplier. The snapshot contains nullable
`accel`, integer `console_window_s = ceil(base_window_s × multiplier)`, and integer
`deadline_budget_s = ceil((base_window_s + 300) × multiplier)`. Require both positive,
representable, and `deadline_budget_s > console_window_s`; reject an absolute deadline that
would overflow the server clock with `configuration_error`. KVM defaults to 900/1200 seconds;
TCG defaults to 9000/12000. An unknown accelerator takes the TCG-safe multiplier.

Both integer fields are bounded by `timedelta.max` in whole seconds. If the configured
window or multiplier cannot produce a finite value inside that bound, the producer returns
`configuration_error`; each deadline creator checks the calendar range against its current
clock before persisting an absolute deadline.

Store the snapshot on the activation `BootPayload.local_timing` and on new release and
conflict `RecoveryRequestV1.local_timing` records. The recovery request's absolute deadline
is the database clock plus `deadline_budget_s`. An idempotent replay retains both its
original snapshot and deadline. The worker uses the snapshot for local activation and
recovery timeouts, and passes it on each local authority mutation request. Activation
preparation and the console poll occupy two handler invocations: the first commits the
activation deadline after preparation, and the immediately continued invocation creates a
fresh client for the poll. Its client deadline is the committed readiness deadline plus
30 seconds of transport-return headroom, converted to the worker's monotonic clock. Retry
never renews the committed deadline. Recovery client calls use no more than the remaining
persisted deadline. Existing payloads without a snapshot keep their historical timing.

The authority request and journal record carry the optional snapshot, and journal replay
requires equality. Before mutation, the local authority validates an explicit `kvm` or
`tcg` accelerator against its owned inactive XML. A `None` accelerator accepts either
domain type but keeps the TCG-safe scale. It also computes its effective console window
from its own settings using the snapshot accelerator and requires exact equality with the
snapshot window. A mismatch raises a local configuration error before mutation. The existing
closed authority transport reports provider failures as `provider_conflict` and logs the local
cause for the operator. The provider uses the validated snapshot window, so no later local read
can silently lengthen it.
Historical requests without a snapshot keep the host-configured window. Remote provider
requests carry no local snapshot and retain their 300-second timeout. Recovery-orphan repair
has no console readiness and retains its 300-second request deadline.

The TCG recovery stop remains hard. Queue latency and preceding recovery work consume the
persisted deadline, and the local provider has no bound on remaining time when it stops the
target. The nominal 300-second scaled margin does not prove a clean stop leaves the full
console window.

## Consequences

New local operations use one frozen budget across server, worker, journal, and provider.
Independent process settings must agree on the effective console window for the operation
to proceed. A configuration change does not lengthen an in-flight operation; a host change
that alters the window refuses it before mutation. A new request with an unrepresentable
deadline fails before enqueue.
A deadline may still expire after queue or transport delay; the existing terminal recovery
behavior applies. The authority request and durable job payload gain optional timing fields;
old records remain readable. The provider-authority host's boot-window setting remains
meaningful for new operations through the equality check and governs historical ones.

## Considered & rejected

- **Recompute each timeout from local configuration.** verified: the server and authority-host
  systemd services use different environment files (`deploy/systemd/system/`, `ebd339c95`), so
  a short server deadline can still wrap a longer provider poll.
- **Use the domain XML accelerator at every layer.** judgment: the server and worker do not own
  that host-local XML; the persisted System accelerator is their established key.
- **Keep TCG recovery's clean stop now that the nominal budget is larger.** judgment: the
  provider cannot prove the remaining time after queue and prior work.
- **Change remote or recovery-orphan timeouts.** judgment: neither uses this local console
  readiness window, and the campaign operator assigned remote timeouts elsewhere.
