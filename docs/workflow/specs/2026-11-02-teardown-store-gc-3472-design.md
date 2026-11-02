# Remote System teardown collects the activation's store files (#3472)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-11-02 by this change. Follows #3419 (PR #3463) and #3467 (PR #3484). Piece 1 of #3472 only;
bounded scans and the accepted-limitation remedy are #3475.

## Problem

`RemoteModuleVolumePreparationStore` (`src/kdive/providers/remote_libvirt/external_boot_authority.py`)
removes an activation's files only in `finalize_cleanup_quarantine`. An activation that is torn
down without a finalized cleanup keeps every file, and a finalized one keeps its
`cleanup-finalized` marker, so the store grows for the life of the host.

## Decision

When `RemoteExternalBootAuthorityAdapter.execute_system_teardown` ends with the teardown record in
phase `complete`, it calls a new store method that removes the torn-down activation's files. The
System's `.teardown` record stays: it is the durable fact that refuses a late stage once the
marker is gone. A `managed` cleanup-quarantine receipt and the recovery record it quarantines
stay; everything else of that activation goes.

## Design

### Late-stage refusal

`cleanup_finalized(binding, plan_identity)` returns true when the marker exists **or** the
System's `.teardown` record (`<system key>.teardown`) is in phase `complete` and its intent names
exactly this `binding` and `plan_identity`. `refuse_finalized` already delegates to it, so
`stage`, `stage_lifecycle`, `reopen_terminal`, `reopen_restored` and the coordinator's
`execute_preparation` refuse a torn-down activation (`reopen_terminal` and `reopen_restored` are reads, and the
error is a `FileNotFoundError` subclass, so their callers still read the record as absent) with `RemoteCleanupFinalizedError`, and the
adapter's `_observe` keeps reporting a cleanup of that activation as `absent` after the marker
is collected. The `complete` checkpoint is written (and fsynced by `_replace`) before collection
starts, so at every point of collection one of the two facts stands. A malformed `.teardown`
record raises `ProviderRecoveryRefusal`, as `begin_system_teardown` already does.

### Collection

`collect_torn_down_activation(binding, plan_identity) -> None` runs under the store's exclusive
lock (below) and unlinks, in this order, each name with `FileNotFoundError` suppressed, then
fsyncs the store directory:

1. `_activation_files(binding, plan_identity)` without the `.terminal` name: lifecycle
   request/completion, `.request`/`.result`/`.completion`, `.preparation`, `.restored`,
   prior-power pair, materialization record and its plan index. Unparseable scan candidates are
   left in place with the existing warning (`_owns`).
2. For each recovery identity named by the recovery index or by the marker's receipt (at most
   two, deduplicated): the `.object-index` files whose content names it (only while the record
   is readable), then, unless the receipt is `managed`, `<identity>.recovery` and then
   `<identity>.cleanup-quarantine`. A record or receipt whose binding or plan differs from the
   activation raises `ProviderRecoveryRefusal`, as finalize does.
3. The recovery index, only while it names one of those identities.
4. The `cleanup-finalized` marker.
5. The `.terminal` record.

Each anchor goes after what it names: the index and the marker go after the record and receipt
they name, so a retry after a crash at any unlink still finds every remaining file. The
`.terminal` record goes after the marker because a marker written before markers carried refs
is readable only while the terminal record stands (`finalized_teardown_refs`). The order
differs from finalize's (index before record) because finalize writes its marker first and can
always find the record through it; a never-finalized activation has no marker.

`execute_system_teardown` calls collection whenever it returns with `record.phase == "complete"`,
including a retry that finds the phase already `complete`, so an interrupted collection
finishes on the next teardown attempt. `observe_system_teardown` does not collect.

`RemoteExternalBootCoordinator.system_teardown_state` returns a state with every record field
`None` when the store's new `torn_down(binding, plan_identity)` predicate (the `.teardown`
check above) is true. A half-collected store would otherwise refuse a retry or an observation:
`teardown_materialization` raises when the plan index outlives its record, and
`finalized_teardown_refs` raises for a ref-less marker once `.terminal` is gone. With the empty
state, `inspect_system_teardown` checks only the domain, overlay and baseline, which a
`complete` record already proved absent, and the facts stay `complete`. A fully collected
store produces the same empty state anyway.

### Adoption exclusion

Nothing serializes a remote adoption (`adopt_recovery_object`) against System teardown (ADR-0787
2026-11-01 amendment). A new store context manager `_exclusive()` opens a fresh descriptor on the
store root and takes `fcntl.flock(LOCK_EX)` on it; closing releases it. A fresh open file
description per acquisition makes two threads of one process exclude each other.
`adopt_cleanup_quarantine` performs its `_replace` (read, compare, replace) inside it, and
collection holds it from its first read to its last unlink. So collection either reads the
receipt already `managed` and keeps it, or removes it before adoption reads, and the adoption
then fails with the existing "changed" refusal.

## Failure model

1. **Actors and deployments.**
   - The remote-libvirt provider authority service, running System teardown, release, and
     recovery-orphan adoption against one store directory on one host.
   - A platform administrator resolving a recovery orphan with `adopt`.
2. **Invariants and assets at stake.**
   - A `managed` receipt and its recovery record survive collection (#3467 boundary).
   - A stage or lifecycle stage for the torn-down activation is refused at every point after
     the `complete` checkpoint.
   - Collection removes only files whose name or content binds them to the torn-down activation;
     other activations' files and the `.teardown` record stay.
   - Collection is repeatable after a crash at any unlink and converges on the same store.
3. **Accepted failure classes.**
   - Other activations of the same System keep their files. Accepted: teardown names one
     activation; a public-safe follow-up candidate.
   - After collection, a recovery-object row for a volume kind cannot be observed through the
     object index. Accepted: finalize already leaves that state (#3326), and teardown removed
     the volumes.
   - A managed receipt and its recovery record stay until the operator removes them. Accepted:
     ADR-0787 2026-11-01 amendment.
   - A second process writing the same store. Accepted: not deployed (ADR-0787 #3419).
   - Once the `.teardown` record is `complete`, a teardown retry or observation no longer
     re-reads the activation's recovery volumes. Accepted: `complete` is written only after
     `inspect_system_teardown` read them absent, and the collected store has no record of them.
4. **Covered elsewhere.**
   - Bounded scans and the accepted-limitation remedy: #3475.
   - Local store collection, a sweep of stores torn down earlier, and teardown lane
     serialization: excluded by the operator for this issue.
   - The finalize-versus-adoption race on the remote store: follow-up candidate, not this
     change.

## Success

1. After a completed teardown of a never-finalized activation, the store holds none of its
   files; the `.teardown` record and another activation's files are unchanged.
2. After a completed teardown of a finalized activation, its marker and remaining files are gone.
3. After a completed teardown of an adopted activation, exactly the managed receipt and its
   recovery record remain of it, and `observe_recovery_object` still returns the adopted
   observation.
4. After collection, `stage` and `stage_lifecycle` for that activation raise
   `RemoteCleanupFinalizedError` and write nothing.
5. A crash at any unlink of collection, followed by a teardown retry, yields the same store as
   an uninterrupted run.
6. Collection and adoption wait for each other's lock; an adoption landing before collection is
   kept, one landing after fails as changed.
7. ADR-0787 carries a 2026-11-02 amendment.

## Validation

- Success 1, 2, 4: `focused-test`, new cases in
  `tests/providers/remote_libvirt/test_external_boot_authority.py`; red before the change because
  the files remain and `stage` succeeds.
- Success 3: `focused-test`, extended `test_remote_system_teardown_keeps_an_adopted_receipt`;
  red under a controlled fault that ignores `managed`.
- Success 5: `focused-test`, crash-at-every-unlink loop modelled on
  `test_remote_finalize_resumes_after_a_crash_at_any_deletion`.
- Success 6: `focused-test`, lock-wait test modelled on the local
  `test_receipt_decisions_wait_for_the_activation_directory_lock`, plus the two orderings.
- Success 7: `task-test-not-applicable`: prose with no executable consumer; `just records` checks
  the record shape.
