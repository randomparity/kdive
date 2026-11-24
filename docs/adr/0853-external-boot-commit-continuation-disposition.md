# 0853 — External-boot commit owns its continuation disposition

## Status

Accepted (2026-11-23)

## Context

[ADR-0845](0845-continue-committed-recovery-checkpoints.md) continues an authenticated
`deadline` or `recovery-attempt` checkpoint on the same running claim, with at most two
handler invocations. That behavior already works. The queue adapter returns a Job or a
classified status, and the worker reconstructs the continuation decision from operation
strings and row state. The commit boundary already knows the typed result and whether the
database applied it or produced a classified row.

## Decision

Return one frozen internal `ExternalBootCommitOutcome(job, continue_claim)` from
`commit_external_boot_authority_result` whenever it returns a Job. Keep non-applied status
enums unchanged. A classified failed/queued reread has `continue_claim=False`. An applied
row continues only when it is RUNNING and the carrier is a typed authority success with a
`DeadlineResult` or `RecoveryAttemptResult`. Missing rows remain SUPERSEDED.

`complete_external_boot` returns that outcome. `fail_external_boot` unwraps its Job to
preserve its current Job-or-status contract. Conflict finalization remains unchanged.
The worker consumes the disposition without an operation-name list. Its authenticated
binding checks, lost-fence/refusal handling, lease behavior and two-invocation bound stay put.
Migrate direct success-adapter consumers to the carried Job and explicit disposition.

This supersedes only ADR-0845's choice to infer the decision from a returned Job without
another internal interface. Its same-claim behavior and bound remain in force. Add only a
supersession banner to that accepted record when this decision is implemented.
No provider result protocol, persisted schema, SQL function, grant or public API changes.

## Consequences

The transaction adapter owns the continuation policy beside its fenced commit result;
the worker owns bounded dispatch. The new outcome is process-local, not durable evidence
or authority. Existing database guards still decide whether a receipt can commit.
No migration or new recovery action is needed.

## Considered & rejected

- **Keep the worker's operation-name predicate.** judgment: it makes the consumer reconstruct
  an adapter decision and couples it to receipt variants already understood by the adapter.
- **Return only a boolean.** verified: direct connected tests inspect final Job state and
  error category after committing; dropping the Job would discard existing needed evidence.
- **Add a provider continuation protocol.** judgment: the existing typed checkpoint receipt
  and fenced database outcome provide the inputs; another provider-facing variant is unnecessary.
