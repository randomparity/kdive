# Commit teardown failures for Systems in their real state (#2881)

## Scope and authority

Campaign scope for issue #2881, token `q2881-7069c372`. The operator approved these exclusions
on 2026-09-28: provider-side teardown failure (#2880); executor threading (#2878, merged);
loosening binding fence predicates (out of scope); the retained-fixture settle, issue criterion 6
(#2880's post-merge live run); manual database or journal edits (not authorized).

[ADR-0620](../../adr/0620-authority-owned-system-teardown.md) governs: public teardown admits the
System in its real state. This change applies that decision to the failure commit, which
migration 0147 missed. It records no new decision.

## Problem

A teardown job's failure goes through `_bound_failure` to `queue.fail_external_boot`, which calls
`commit_external_boot_authority_result` with result operation `fail`. That function's `fail`
precondition for `p_purpose = 'teardown'`
(`src/kdive/db/schema/0122_external_boot_authority.sql`, the `v_operation = 'fail'` block) still
requires `system.state = 'failed'` and activation state `recovery_conflict` or `recovery_failed`.
Migration 0147 widened only the allocation clause and the success finalizer
(`finalize_external_boot_authority_teardown`). So every teardown failure for a System that is not
`failed` returns `superseded`, the worker logs `external boot job … was reclaimed; result dropped`,
and the job stays `running` until its lease lapses.

After migration 0128 the function already separates most losses: an activation, System or Run
identity mismatch returns `observed_identity_stale`, and an authority, marker or acknowledgement
mismatch returns `authority_superseded`; both fail the job. `superseded` remains for an unknown
worker credential, a missing allocation, a lost job attempt, and the per-operation state
preconditions. The worker reports all four as a reclaim.

Evidence: a read-only query of the retained fixture showed System `ready`, activation
`activating`, teardown authority generation 3 `current`, job `running` at attempt 2 of 3, and a
`ready` reservation. The reclaim in the issue body is incidental: the binding fields matched, and
the state precondition rejected the commit.

## Design

1. **Migration `0160_external_boot_teardown_failure_commit.sql`** edits
   `commit_external_boot_authority_result` by exact text replacement, the pattern 0147 uses, and
   raises if either old text is absent:
   - The `fail`/`teardown` state clause becomes the 0147 allocation clause: System state in
     `provisioning, ready, reprovisioning, restoring, paused, crashing, crashed, failed`;
     activation state in `preparing, prepared, activating, active, recovering, recovered,
     recovery_conflict, recovery_failed, abandoned`; and no newer activation for the System
     (same `(created_at, id)` ordering). A `torn_down` System or activation stays superseded.
   - The terminal-failure Run update gains `AND p_purpose <> 'teardown'`. Teardown success never
     writes the Run; before this change a teardown failure could not reach that update with a
     `created` or `running` Run, because only `failed` Systems passed. Widening the states would
     otherwise let a teardown failure mark an in-flight Run failed.
   The binding fence (worker, job attempt, authority generation, marker, acknowledgement,
   counter) is untouched.
2. **Worker diagnostic.** On `ExternalBootCommitStatus.SUPERSEDED`, the worker calls a new
   `queue.external_boot_attempt_is_running(conn, job) -> bool`, which reads whether `jobs` still
   has this job `running` at this attempt. A claim increments `attempt`, so `False` means a real
   reclaim or finalization and the worker keeps the existing line byte for byte (live tests
   match it). `True` logs `external boot job %s attempt %s is still running but its commit was
   refused (worker credential, allocation or state precondition); result dropped`. The read is
   post-transaction and advisory only.

Rejected: a new SQL status for "precondition mismatch", because it changes the commit function's
return contract and the worker's classification map for a diagnostic; and returning which
predicate failed, because it needs a rewrite of the ~40-predicate fence the operator excluded.

## Failure model

1. **Actors and deployments** — worker processes committing external-boot authority results in
   the local-libvirt and remote-libvirt deployments; the migration runner.
2. **Invariants and assets at stake** — the binding fence stays exact; one job transition per
   attempt; reservation credit stays exactly once (the `fail` path writes no reservation or
   release row); an in-flight Run is never failed by a teardown failure.
3. **Accepted failure classes** — the diagnostic read can race a concurrent reclaim and log the
   refused-commit line for what became a reclaim: accepted, log-only, no state effect. A migration
   applied over a hand-edited function raises and aborts: accepted, the shape guard's purpose.
4. **Covered elsewhere** — why the provider teardown fails (#2880); settling the retained
   fixture (#2880's live run); the unused `teardown` success clause in the same function, which
   the worker never reaches because success commits through the finalizer (follow-up candidate).

## Validation

- DB test: seed a teardown case at System `ready` with activation `prepared`, and again with
  `activating`; allocate, acknowledge, and commit a non-terminal `fail`. Expect `superseded`
  before the migration and `('applied', 'queued')` after. Also expect terminal `fail` gives
  `('applied', 'failed')` with the Run unchanged, and a newer activation still gives
  `superseded`.
- Worker tests: `SUPERSEDED` with the attempt still running logs the refused-commit line; with a
  reclaimed attempt logs the unchanged reclaim line.
- Criterion 6 is met by #2880's post-merge fixture settle, not by this change.
