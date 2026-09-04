# 0595 — External-boot re-entry uses provider receipts

## Status

Accepted (2026-09-04)

This decision supersedes ADR-0592's cleanup-retry and tombstone reconstruction conclusion:
the request-bound cleanup tombstone now carries the authenticated recovery point needed to
reconstruct positive evidence after an authority-process restart. ADR-0592's anchored journal
proof and finalization rules remain in force.

## Context

ADR-0593 assigns materialize and prepare to server preparation and the remaining lifecycle
mutations to marked worker jobs. A process can stop after any provider return and before the
corresponding Postgres commit. Repeating a mutation blindly is unsafe, while skipping it without
provider evidence can strand the activation.

The provider authority already journals worker mutation intent and completion and exposes a closed
full-state observation. The worker handlers currently bypass that execution lane and call
`ProviderRuntime.external_boot` directly. Server preparation has no equivalent receipt.
`ExternalBootPorts.observe` reports only a running kernel and cannot prove recovered definition,
modules, power state, or cleanup absence.

## Decision

Every re-entry decision uses provider-owned durable evidence.

Marked worker jobs execute through the provider authority's mutation endpoint after takeover
acknowledgement. The authority journal is their receipt. A retry presents the same stable operation
identity, and the authority returns or reconstructs the recorded observation before deciding
whether a commit point remains. Worker code does not call a mutating runtime port directly.

Server preparation uses a provider-neutral preparation seam with separate observe and execute
operations. Providers persist a closed preparation observation, bound to plan, System, Run,
activation, authority, and operation identity, before returning from materialize or prepare.
Observation returns absent, materialized, or prepared and carries the exact closed values core must
commit. Equal replay returns the receipt; a conflicting identity fails closed.

Operation success is judged from complete provider state: target for activation, source plus prior
power condition for recovery, and accounted absence for cleanup. Running-kernel identity remains
one component of activation readiness; it is not the recovery or cleanup receipt.

## Consequences

- A crash after provider success and before Postgres commit converges without another provider
  mutation.
- The authority host remains the worker mutation boundary established by ADR-0584.
- Providers must store preparation receipts with the recovery objects they already own and remove
  them only after cleanup is durably accounted.
- Fault-inject implements the same receipt state and can prove all six interruption boundaries
  without a hypervisor.
- Local-libvirt persists the receipt using its existing descriptor-relative atomic store rules.
- Remote-libvirt stays unadvertised and unchanged here. Issue #2200 owns its authority adapter,
  composition, and adoption of this provider-neutral receipt contract.
- ADR-0593's prepared-before-admission ownership remains unchanged. This decision supplies its
  missing restart contract rather than moving materialize or prepare into worker jobs.

## Considered and rejected

- **Repeat every provider method and require it to be internally idempotent.** This may prevent a
  duplicate effect but cannot satisfy at-most-once mutation or distinguish a completed operation
  from an absent one.
- **Write a Postgres intent before the provider call.** Intent proves that core planned a mutation,
  not that the destination completed it, so it cannot authorize either redo or skip after loss.
- **Use running-kernel observation for every phase.** It says nothing about materialization,
  recovery definition/modules/power, or cleanup absence.
- **Add a second worker-side receipt table.** The provider authority journal already owns this
  evidence and is fenced to the provider mutation. Duplicating it creates two authorities for the
  same event.
