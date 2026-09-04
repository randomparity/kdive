# External-boot idempotency and bounded failures — design

Issue: [#2202](https://github.com/randomparity/kdive/issues/2202). Parent: #2118. Governing
decisions: [ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md) and
[ADR-0593](../../adr/0593-external-boot-operations-ride-marked-boot-and-teardown-jobs.md).

## Goal

An authority-marked external-boot job may be replayed after worker loss without repeating a
provider mutation that already took effect. Each retry observes durable activation state and the
provider destination, reuses persisted deadlines, advances the recovery-attempt ledger, handles
losing compare-and-set outcomes, and exposes only a closed failure vocabulary.

No new architectural decision is introduced. ADR-0583 already requires observation-driven
re-entry, absolute persisted deadlines, recovery-attempt terminalization, and conflict-deadline
replacement. ADR-0593 fixes the marked-job/authority seam and assigns its mid-operation commits to
this issue. Migration 0128 makes that existing result contract persist the bounded recovery reason
and action which `jobs.get`/`jobs.wait` already surface from `jobs.failure_context`.

## Existing boundaries

- `run_operation` owns provider resolution, activation validation, authority allocation,
  acknowledgement, provider execution, and failure wrapping.
- `commit_external_boot_authority_result` is the worker's only authorized activation write path.
  Its `deadline` and `recovery-attempt` results deliberately keep the job running.
- `ExternalBootActivationRepository` keeps authority predicate failures opaque. Differentiation is
  a caller decision made from activation, reservation, attempt, and marker facts already visible to
  the worker; the repository's `CasStatus` contract is unchanged.
- `ExternalBootPorts.observe` is the only provider-neutral observation available to a worker.
- Materialization and preparation remain prepared-before-admission under ADR-0593. Re-entry tests
  cover worker loss after each persisted materialization/preparation boundary by seeding those
  durable facts; the authority-marked worker never repeats those server-owned provider calls.

## Re-entry protocol

The runner reads the activation and its ready reservation before allocation. It classifies a
non-admissible retry without invoking a provider:

1. A mismatched activation identity is `observed_identity_stale` and directs the caller to
   `systems.get`.
2. An activation that still needs capacity but has no ready reservation is
   `reservation_not_ready` and directs the caller to `jobs.wait`.
3. A marker whose authority generation/owner has lost the row is `authority_superseded` and
   directs the caller to `jobs.get`.

These are failure-context reasons, not new `ErrorCategory` values. They map to existing
`stale_handle`, `infrastructure_failure`, and `stale_handle` categories respectively. The failure
context contains only `phase`, `reason`, and `next_action`; it never includes provider exception
text, paths, host identifiers, or object identifiers.

After acknowledgement, operations that can safely determine completion call `observe` before a
mutation. Activate skips `activate` when the observed running kernel already equals the persisted
materialization. Recover and conflict resolution similarly skip `recover` when the observed kernel
already proves the operation's postcondition. A non-matching or unavailable observation permits
the mutation only while the persisted deadline remains open. Cleanup stays idempotent at the port
contract: it calls the provider cleanup operation, whose implementations must accept already-absent
owned objects; there is no provider-neutral deletion observation in `ExternalBootPorts`.

Every successful post-mutation path observes again before building terminal evidence. A provider
raise is converted once to `ExternalBootAuthorityFailure`; unknown exceptions map to
`infrastructure_failure`. Known `CategorizedError` values retain their category only when the SQL
contract accepts it; otherwise they use the same bounded substitute. The explicit category tuple
is pinned against migration 0128.

## Deadline and recovery ledger protocol

Activation uses `activation_readiness_deadline` from the activation row whenever present. Only its
first commit computes an absolute UTC deadline from the worker reference clock. A retry at or past
that instant does not activate again: it begins or resumes recovery and ultimately returns a
terminal failure carrying `boot_timeout` if readiness is not proved.

Recovery uses the current attempt's persisted `recovery_readiness_deadline`. The first ordinary
recovery creates one attempt with a deterministic attempt identity derived from the admitted
operation identity and commits it through the existing `recovery-attempt` authority result before
provider mutation. A retry reuses that row. At or past its deadline, the handler finishes the
attempt as `recovery_failed`, retaining the activation's recovery point and pre-recovery evidence.

Conflict resolution is the one deadline replacement edge. It creates a new attempt and computes
its deadline from that operation's server time, so time parked in `recovery_conflict` is excluded.
The previous conflict attempt remains immutable in the ledger.

All compare-and-set results are consumed. `APPLIED` continues. `NOT_FOUND` becomes
`observed_identity_stale`. `SUPERSEDED` is differentiated from a same-lock reread into
`reservation_not_ready` or `authority_superseded`. No `IllegalTransition` escapes the handler; an
unexpected transition is converted to a bounded commit-phase failure.

## Migration 0128

Migration `0128_external_boot_reentry_failures.sql` replaces
`commit_external_boot_authority_result` without changing its signature or privileges. It accepts
only the three reason/action pairs above in `failure_context`, in addition to the existing optional
phase, and stores them on a terminal job. Retryable failures continue to clear failure context when
requeued. The migration also makes repeated `deadline` and `recovery-attempt` commits idempotent:
an equal persisted value applies, while a different value is superseded. A recovery-attempt replay
cannot append a second ledger row.

Rollback is the normal forward-only migration rule: revert Python callers first; the widened
validation remains compatible with old phase-only failures. The SQL function cannot be rolled back
by deleting migration history.

## Fault injection and tests

The fault-inject port gains per-operation call counts, configured faults before or after mutation,
and stable observation state. Tests prove:

- replay after the materialization, preparation, activation, release, recover, and cleanup durable
  boundaries preserves activation and attempt rows apart from `updated_at` and invokes each
  provider mutation at most once;
- observation occurs before a redo decision and a matching observation skips mutation;
- each `CasStatus` is consumed and concurrent state movement never leaks `IllegalTransition`;
- activation and recovery deadlines are reused and expire into the required terminal states;
- conflict resolution starts a fresh deadline from its own clock;
- every injected provider fault maps to the explicit category tuple, and serialized failures carry
  no raw exception, host, or path text.

Focused integration tests use disposable Postgres. No native ppc64le proof is required: all changed
contracts are provider-neutral and exercised through fault-inject on the x86_64 host.

## Security model

No entry point or authorization grant changes. The existing authority acknowledgement and SQL
generation fence remain the trust boundary. Provider output and exceptions cross from an
operator-configured runtime into the worker; the control is a closed model plus constant failure
context, with no exception chaining. Marker and activation facts cross from persisted queue input;
the control is the existing binding validation plus same-System-lock reread before a CAS reason is
selected. The design does not authenticate provider transports or add provider adapters; those are
owned by their existing issues.
