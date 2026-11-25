# External-boot operation collaborator ownership

## Problem

`OperationContext` declares connection, authority executor and incarnation credential,
but lifecycle code also transports them through an untyped prerequisite mapping.
Factory-selected clients require a second injection path, and release completion
copies the mapping again to add a credential digest and deadline.

## Scope

Use the existing context fields for executor and SQL connection. Remove runner's
factory-only prerequisite executor injection, lifecycle's `_with_executor` wrapper,
and `_release_prerequisites`; pass the existing admission callbacks directly.
Keep factory client selection and context construction unchanged. Teardown retains
its SQL admission validation but returns no connection entry; its completion and
module-reap paths use `context.connection`.
Give `_run_active_release` explicit keyword-only `credential_digest: bytes` and
`deadline: datetime` arguments. Release completion hashes the context credential
and passes the existing fresh-versus-request deadline directly. Keep reservation,
attempt deadline, release identity and teardown identity as prerequisite evidence.
Preserve recovery-before-cleanup ordering, recovered-state recovery bypass, cleanup
receipt adoption/fallback, conflict results, superseded failures, SQL text/argument
order and typed completion. Add no context wrapper, metadata schema or protocol.

### Failure model

- Prevent: operational collaborators having two owners and factory routing
  depending on a second conditional overwrite of prerequisite state.
- Detect: wrong connection/credential digest/client selection, changed first/later
  attempt deadlines, release ordering or superseded/receipt behavior.
- Recover: revert the local plumbing change; no database migration is needed.
- Accepted: domain evidence remains in the existing mapping; this unit does not
  redesign the lifecycle state machine or transaction/journal policy.

## Success

1. Operational collaborators have the existing typed context as their one owner.
2. Derived release values are explicit inputs without copying prerequisite state.
3. Existing admission, SQL and lifecycle behavior remain unchanged.

## Validation

- `Mode: focused-test`: observe the release context at its execution boundary and
  reject operational collaborator entries in prerequisite evidence; this is the
  expected structural red before removing duplicate plumbing. Extend real DB-backed
  reentry tests to assert connection, credential digest and factory-selected executor,
  retaining first/later deadlines. Exercise `test_release_connected.py` for real Worker/authority/database release,
  interruption, retry and receipt adoption. Add checked SQL-boundary branch cases
  for recovered bypass, receipt fallback, conflict and superseded failures.
- `Mode: task-test-not-applicable`: this routing cleanup adds no SQL/state algorithm;
  compare the affected SQL strings/argument ordering and review the removed wrappers.
- Run the release-reentry/lifecycle/runner and connected-release files with
  `just test-verbose` (database skips do not prove behavior), then `just lint`,
  whole-tree `just type`, documentation checks and configured hooks.
