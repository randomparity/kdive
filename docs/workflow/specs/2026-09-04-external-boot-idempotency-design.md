# External-boot idempotency and bounded failures — design

Issue: [#2202](https://github.com/randomparity/kdive/issues/2202). Parent: #2118. Governing
decisions: [ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md),
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md),
[ADR-0593](../../adr/0593-external-boot-operations-ride-marked-boot-and-teardown-jobs.md), and
[ADR-0595](../../adr/0595-external-boot-reentry-uses-provider-receipts.md).

## Goal

Every external-boot phase is restartable after the provider returned but before core committed.
Re-entry observes provider-owned durable evidence before deciding whether to call a mutating port.
Retries reuse persisted deadlines, consume every compare-and-set result, advance the recovery-attempt
ledger, and expose only a closed failure vocabulary.

## Two execution lanes

The lifecycle has two owners and therefore two receipt shapes.

The server owns `materialize` and `prepare`. A new `ExternalBootPreparationPorts` seam exposes
`observe_preparation(request)` and `execute_preparation(request)`. The request carries the plan,
activation binding, authority reference, and stable operation identity. The result is a closed
`ExternalBootPreparationObservation`: `absent`, `materialized`, or `prepared`, with the exact
materialization and recovery point when present. Providers persist the observation before returning.
An equal operation identity replays the stored result; a different identity for the same activation
is a conflict. The server preparation service observes first, performs only the missing phase, and
records each returned value through `ExternalBootActivationRepository`. A process loss after either
provider return therefore re-observes the provider receipt and commits it without a second mutation.

The worker owns activate, recover, resolve-conflict, release, cleanup, and teardown. After takeover
acknowledgement it sends `AuthorityMutationRequestV1` through a widened
`ExternalBootAuthorityExecutor.execute`. The authority service already observes the provider,
journals mutation intent and outcome, and returns `AuthorityObservationV1`; it is the only worker
path that may invoke the provider adapter. `run_operation` no longer calls
`ProviderRuntime.external_boot` directly. On retry, the authority journal either returns its
terminal observation or observes the destination before deciding whether another commit point is
needed. This is the durable receipt for worker phases.

The worker result is built only from an observation whose category satisfies the operation:
`target` for activate, `source` for recover, and the provider's accounted-absence observation for
cleanup. `mixed`, `conflict`, and `unreadable` never become success. Release has no provider mutation;
its database result is naturally idempotent. The authority observation includes definition, module,
power, and owned-object state through its closed category and composite digest, so recovery is not
proved from running-kernel identity alone.

## Preparation adapters

Fault-inject stores preparation observations in memory and exposes deterministic before-return and
after-receipt fault points plus per-phase mutation counts.

Local-libvirt stores a canonical `preparation-result.json` beside the existing activation recovery
metadata. Publication uses the existing descriptor-relative, no-follow, atomic-write discipline.
The record binds the operation identity, plan identity, activation ownership, materialization, and
recovery point. It is written only after the corresponding host operation has reached its existing
durable phase. Observation rejects malformed, foreign, or mismatched records. Cleanup removes the
receipt with the activation's other owned objects only after its result is durably accounted.

Remote-libvirt does not implement the preparation seam in this issue. Its external-boot authority
adapter, composition, and provider semantics are owned by #2200, and that provider remains
unadvertised. The provider-neutral contract is available for that owner to adopt without reserving
or pre-building remote implementation here.

## Re-entry and CAS classification

Before mutation, the handler reads the activation and ready reservation under the System lock.
Every `CasStatus` is consumed:

- `NOT_FOUND` or a mismatched activation identity becomes `observed_identity_stale`, terminal for
  this job, with next action `systems.get`;
- a state that can progress but has no ready reservation becomes `reservation_not_ready`,
  non-terminal and requeued without changing either deadline, with next action `jobs.wait`;
- a lost operation owner or authority generation becomes `authority_superseded`, terminal for this
  job but not for the activation, with next action `jobs.get`.

These are closed failure-context reasons, not new `ErrorCategory` values. They map to
`stale_handle`, `infrastructure_failure`, and `stale_handle`. No `IllegalTransition` escapes the
handler. Migration 0128 validates the three exact reason/action pairs and their terminal/requeue
shape, and makes equal deadline and recovery-attempt commits idempotent.

## Deadlines and recovery attempts

Activation uses the row's `activation_readiness_deadline` whenever present. Only the first
`prepared -> activating` result computes it from the operation's server time. At expiry, the handler
does not invoke activate; it enters recovery using the retained recovery point.

Ordinary recovery uses the current attempt's persisted `recovery_readiness_deadline`. The first
edge creates a deterministic attempt identity and commits the `recovery-attempt` result before the
provider mutation. Retry reuses that attempt. Expiry finishes it as `recovery_failed` while leaving
the recovery point and pre-recovery evidence readable.

Conflict resolution is the sole replacement edge: its new attempt computes a new deadline from the
resolving operation's `server_time`, so time parked in `recovery_conflict` is excluded. Previous
attempts remain immutable.

## Failure mapping

Authority transport errors, provider observation categories, and fault-inject failures map once to
`ExternalBootAuthorityFailure`. The failure context contains only closed `phase`, `reason`, and
`next_action` fields. Unknown exceptions become `infrastructure_failure`; raw exception text,
provider paths, host identifiers, and chained exceptions never cross into the result. Tests pin the
complete category tuple accepted by migration 0128 and enumerate every injected fault.

## Migration 0128

`0128_external_boot_reentry_failures.sql` replaces
`commit_external_boot_authority_result` without changing its signature or privileges. It validates
the reason/action/terminal combinations, accepts exact replay of persisted deadlines and attempts,
and refuses conflicting replay as superseded. Rollback follows the forward-only migration rule:
revert callers first; the widened validation remains compatible with phase-only failures.

## Proof

Focused Postgres and fault-inject tests interrupt after each of materialize, prepare, activate,
release, recover, and cleanup returns but before core commit. The retry must produce equal activation
and attempt rows except `updated_at`, and each provider mutation counter must remain one. Separate
tests pin observe-before-redo, all CAS outcomes, both deadline families, conflict deadline reset,
total bounded error mapping, and serialization redaction.

Local-libvirt contract tests run on this host and exercise the preparation receipt plus authority
journal replay. The native ppc64le live tier is excluded by campaign authority. No MCP tool contract
or reconciler lane changes in this issue.

## Security model

No entry point or authorization grant changes. Preparation receipts accept only closed values and
are bound to plan, System, Run, activation, and operation identity before reuse. The authority host
remains the sole worker-side mutation boundary. Provider output is reduced to closed observations;
free-form failures remain in operator logs and are raised without chaining across the boundary.
