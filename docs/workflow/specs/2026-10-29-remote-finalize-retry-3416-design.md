# Finalize an adopted release cleanup under the proof it ran with (#3416)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended. No new
decision, migration, wire or adapter-interface change.

## Problem

A remote release whose first CLEANUP is interrupted after the quarantine receipt is recorded, and
before finalize, fails on retry with `remote cleanup proof differs from the quarantine receipt`.

## Facts verified in source (main 5c3686fd6)

- A release phase's `operation_identity` hashes the authority generation and the worker
  incarnation (`jobs/handlers/external_boot/lifecycle.py` `_release_phase`), and its `attempt_id` is
  derived from it. A retry under a later generation has a new identity and attempt.
- The service adopts the earlier generation's terminal CLEANUP for that retry
  (`adopted_release_phase`), so the provider commit does not run again. `commit_cleanup`
  never re-records the receipt; it keeps the identity, attempt, journal sequence and digest
  of the cleanup that ran.
- The service then calls `finalize` with a context built from the retry's own `mutation-started`
  record. Every receipt field differs, so the remote check fails. The same-generation replay path
  already matches, because it reuses the original record.
- The local finalizer compares the tombstone's owner and point digest, not the journal position
  (`finalize_tombstone`), so it is unaffected.

## Options

- **Re-anchor the receipt on the retry.** Rejected. The quarantine row published from attempt 1
  carries the receipt's journal digest and the object port fences `delete`/`adopt` on it.
  Rewriting the receipt invalidates the evidence the resolver acts on, and `commit_cleanup`
  is not on this path to do it.
- **Loosen the remote `finalize` comparison.** Rejected. The retry's identity is a hash the adapter
  cannot relate to the receipt's, so the only available rule is to accept another operation's
  receipt.
- **Give `finalize` the proof of the cleanup that ran (chosen).** The service already knows which
  record it adopted. `_finalize_adapter` selects the `mutation-started` record of the adopted
  operation instead of the retry's, so the context equals the receipt and the adapter's exact
  check is unchanged.

## Design

`_finalize_adapter` looks for the earlier generation's terminal record of the same release phase
(the lookup that decides adoption, now one helper `_prior_release_terminals`). When there is one
for a release RECOVER or CLEANUP, the context is built from the `mutation-started` record of the
oldest such terminal: a retry that adopted the cleanup and was itself interrupted leaves a later
terminal, but only the oldest ran the commit that wrote the receipt. Otherwise nothing changes. The remote adapter and the `AuthorityMutationFinalizer`
protocol are untouched; the local finalizer ignores the journal fields.

## Not changed (reported as follow-ups)

- `cleanup_quarantine_inventory` still requires the request's attempt to equal the receipt's, so a
  retry aborted again before `finalize` publishes against attempt 1's receipt and answers
  `provider_conflict`.
- `finalize` ignores `receipt.managed`; a resolver-adopted receipt can be finalized by a retry.

## Tests

`tests/providers/external_boot_authority/test_service.py`: an adopted cleanup finalizes with the
first generation's operation identity, attempt, sequence and digest.
The live cells are re-proved by the #3339 worker.
