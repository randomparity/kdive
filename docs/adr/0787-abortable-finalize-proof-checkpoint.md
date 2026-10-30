# 0787 — Abortable before-finalize proof checkpoint

## Status

Accepted (2026-10-19)

## Context

`ops.resolve_recovery_orphan` acts on `external_boot_recovery_quarantine` rows. Only the
authority writes one: when cleanup finalization fails after the terminal journal record,
`_publish_cleanup_quarantine` re-reads the private receipt and publishes it through the
head-fenced SQL function ([ADR-0615](0615-authority-owned-orphan-disposition.md)). On a healthy
host that failure does not happen. The [ADR-0622](0622-native-authority-fault-proof-barrier.md)
barrier pauses only around the provider commit, and a restart there recovers through
`_finish_recovery` and quarantines nothing. Issue #3185 needs a real quarantined orphan for live
proof. It forbids synthetic database writes and unreviewed proof-control expansion.

## Decision

We will extend the ADR-0622 barrier in three ways.

- **`before-finalize` checkpoint.** An arm may name it only with operation `cleanup`.
  `_finalize_adapter` awaits it just before `adapter.finalize`, outside the `try` that maps
  provider errors.
  - `ProofBarrierAbort` is re-raised through `_provider_error` as `provider_conflict`, so the
    caller publishes the quarantine through its unchanged fences.
  - `ProofBarrierShutdown` propagates unmapped, so a service shutdown never publishes one.
- **`abort` action.** It is valid only while the single arm is `reached` at `before-finalize`.
  It fails that one wait with `ProofBarrierAbort` and clears the arm.
- **`release` of a never-reached arm clears it.** A later matching checkpoint finds no arm and
  passes, which is the same effect as the pre-release had. `checkpoint()` clears its arm in a
  `finally`.

These ADR-0622 properties are unchanged:

- the socket is root-only by `SO_PEERCRED`;
- only one arm exists at a time;
- requests are closed JSON of at most 1024 bytes;
- no provider, path or command is selected;
- shutdown never releases a wait.

## Consequences

- The local installed authority can produce one real quarantine row on demand, bound to an exact
  System, Run and cleanup operation.
- The release job's next attempt finalizes normally and removes the receipt. The orphan the
  resolver then sees is already absent at the provider, so the resolver's delete of a present
  receipt stays unproven.
- The remote authority has no finalizer, so this checkpoint never fires there.
- The pause happens inside the System's authority lane lock. While it is held, other requests for
  that System wait, the recovery-orphan serializer included. The proof bounds the pause to 120 s.
- An abort costs no service restart.
- An arm that was armed and then released, but never reached, no longer strands the barrier as
  `busy`.

## Considered & rejected

- **Record the local orphan as blocked too.** judgment: this leaves authority publication
  unprovable on every lane, when a bounded seam reaches the real publisher.
- **Pause at `before-finalize` and restart the authority.** verified: both authority unit
  templates set `StartLimitBurst=5` and `StartLimitIntervalSec=600`
  (`deploy/ansible/roles/*/templates/*authority*.service.j2` at `b2d93172e`). Under this ADR the
  shutdown path also deliberately publishes nothing.
- **Perturb the private tombstone directory so `finalize_tombstone` raises.** judgment: this
  couples the proof to private layout, and the same entry would also break the resolver's
  delete.
- **Seed quarantine rows with SQL.** verified: migration 0144 lets only
  `kdive_provider_authority` execute `publish_external_boot_recovery_quarantine_authority`. A
  seeded row also proves no authority publication.
- **Keep the arm armed across retries, so the receipt survives to resolution.** judgment: under
  ADR-0714 the second identical `provider-conflict` ends the release job, and recovering it is
  #3143's path, which #3185 excludes.
- **Saturate the provider offload pool.** verified: `_OFFLOAD_CAPACITY = 4` is a non-blocking
  admission (`src/kdive/providers/local_libvirt/external_boot_authority.py`, at `b2d93172e`).
  Filling it depends on timing, which ADR-0622 rejected.

### Amendment (2026-10-27): the remote authority finalizes and quarantines cleanup (#3326)

This amendment replaces the Consequences bullet "The remote authority has no finalizer, so this
checkpoint never fires there". The remote-libvirt authority now fires the checkpoint and
publishes a quarantine row like the local one.

- After release cleanup, `commit_cleanup` records a private `cleanup-quarantine` receipt in the
  module store, named by the recovery record's identity. It binds the operation identity, the
  attempt and the anchored `mutation-started` journal sequence and digest.
- `finalize` runs for `cleanup` only. It requires the receipt to equal the anchored proof, writes
  a `cleanup-finalized` marker, then removes the recovery record, the indexes that name it and,
  last, the receipt. It is repeatable until only the marker remains, and a missing receipt is a
  no-op. A cleanup for another operation is refused before any provider call while a receipt
  stands.
- `cleanup_quarantine_inventory` reports the one unmanaged `recovery-record` observation whose
  operation identity and attempt equal the request's. Any other receipt is a conflict.
- The remote adapter serves `observe`, `delete` and `adopt` for `recovery-record` under digest
  fences. `delete` finalizes the record. Other object kinds are refused, not routed elsewhere.
- The remote branch of the authority host builds the orphan service over the remote adapter, not
  the local one.
- The marker keeps a finalized cleanup distinguishable from a record that never existed, so a
  later cleanup `observe` classifies `absent`, as it did while the record remained. It also
  locates the receipt once the recovery index is gone. The object port and inventory report the
  receipt actually on disk, whatever the marker says, so an interrupted finalize never hides
  residue. System teardown already treats an absent record as `recovery_absent`.

### Amendment (2026-10-27): remote finalize also removes the activation's other files (#3333)

After the marker, remote `finalize` removes the activation's remaining per-activation files (terminal, restored, prior-power, preparation, materialization, lifecycle and request records) before the recovery record, keeping the System `teardown` record. The marker is canonical JSON carrying the module volumes, the boot artifact refs and the PREPARE nonce, so System teardown proves the same set absent after finalize; a malformed marker, or a marker from before it carried refs once the terminal record is gone, makes teardown refuse rather than prove nothing, and nothing rewrites such a marker (it exists only from the unreleased #3367 build). A later read, stage or prepare for a finalized activation raises `RemoteCleanupFinalizedError`.

### Amendment (2026-10-29): an adopted cleanup finalizes under the proof it ran with (#3416)

A retried release generation adopts the earlier generation's terminal cleanup, so `commit_cleanup`
does not run again and the receipt keeps that cleanup's operation identity, attempt and anchored
journal position. The service therefore builds the `finalize` context from the adopted operation's
`mutation-started` record (the oldest, when several retries were interrupted), not the retry's, and the remote adapter's exact comparison with the
receipt holds. The receipt is not rewritten, so the published quarantine row and the object port's
digest fences keep naming what the first attempt recorded.

### Amendment (2026-10-30): release finalize leaves an adopted receipt in place (#3435)

`ops.resolve_recovery_orphan` with the `adopt` disposition marks the cleanup quarantine receipt
`managed`, and the quarantine row becomes `adopted`. A later attempt of the same release job still
reaches `finalize`. Before this amendment, both providers then removed the adopted receipt and the
records it proves. The remote adapter never read `managed`, and the local finalizer unlinked the
receipt with the tombstone.

- Release `finalize` makes no change while the standing cleanup receipt is `managed`. Remote
  `finalize` returns before its proof comparison. Local `finalize_cleanup_tombstone` reads the
  receipt under its pinned session and returns. An unmanaged or absent receipt finalizes as
  before.
- The two providers agree at `finalize`. Inventory, observation, recovery-object `delete` and now
  release `finalize` all leave a managed receipt alone, so release does not make the `adopted`
  row stale.
- System teardown's discard path (ADR-0583) is unchanged: destruction still owns its cleanup.

Considered & rejected:

- **Keep removal, and have the resolver's adopt warn that a retry may still finalize.** judgment:
  this leaves the database recording `adopted` for an object the provider later deletes, and it
  makes an audited administrator decision depend on whether a retry happens to follow.
- **Raise a conflict on a managed receipt.** verified: `rg -n '"managed": (False|True)' src/kdive`
  at `37e2db046` finds only the two adopt writes setting `True`, plus the remote marker's
  `False` copy, which never rewrites the receipt. No resolver path un-adopts a receipt, so every
  later retry would fail the same way, and under ADR-0714 the second identical
  `provider-conflict` ends the release job.

### Amendment (2026-10-30): an adopted cleanup publishes its quarantine under the proof it ran with (#3434)

The #3416 amendment applied the adopted proof to `finalize` only. A retried release generation
that is aborted again before `finalize` still published under its own CLEANUP identity. The
receipt names the adopted cleanup's identity, so the remote inventory refused it, and the
repository and publish function refused it too. A shutdown at the first `before-finalize` publishes
nothing. When an aborted retry followed it, no row could be published at all.

- The service derives one proof context, the adopted operation's `mutation-started` record (the
  oldest, or the request's own when nothing was adopted), and passes it to both `finalize` and
  `cleanup_quarantine_inventory(request, context)`. The remote inventory compares the receipt's
  operation identity and attempt with that context instead of the request. The local
  adapter takes the parameter and keeps its request comparison. That gap is a follow-up.
- Each published object carries its own operation identity. Migration 0186 stores that identity on
  the row and compares an existing row against it. It is admitted when it equals the current
  authority's identity, or, for a `release` authority only, when it equals the 0143 `cleanup`
  derivation of an earlier acknowledged `release` authority row of the same system, activation,
  run, plan, provider kind and authority instance. Every other identity is refused. An object with
  no identity falls back to the current authority's, so a binary older than 0186 publishes as before. The row
  therefore names what the receipt names, and the resolver's binding equals the object port's.

Considered & rejected:

- **Return no observation from a retry whose receipt belongs to an adopted operation, relying on
  the earlier generation's row.** judgment: this leaves a shutdown followed by an aborted retry
  with no row, and the issue requires a row however many times the release was interrupted.
- **Do nothing.** judgment: an aborted retry stays unresolvable, and the issue requires a row.
- **Store the retry's identity on the row and have the resolver translate it.** verified: the
  resolver refuses unless the provider's observed binding equals the one it rebuilds from the
  row (`src/kdive/providers/external_boot_authority/orphan.py`, `orphan_binding_changed`). The
  remote observation names the receipt's identity, so a row stored under the retry's identity
  could not be resolved without changing the disposition path.
- **Accept any caller-supplied identity in the publish function.** judgment: a
  `SECURITY DEFINER` boundary would then trust the authority worker for a value the database
  can derive itself.

### Amendment (2026-10-30): remote finalize removes only what it can prove and names the remedy (#3419)

The #3326 and #3333 reviews left six low findings on remote `finalize_cleanup_quarantine`. Each is
now fixed. The #3435 and #3434 amendments are unchanged: a `managed` receipt still returns before
any of this runs, and the receipt is still compared with the derived proof context.

- The recovery index is removed only while its content names the record being finalized. The
  check no longer depends on the record still being present, so a retry after the record went
  first also removes the index. An index naming another record stays in place.
- Each finalize directory scan refuses more than 4096 names of the suffix it reads, before reading
  any of them and before the marker is written. The error tells the operator to release or tear
  down the host's other activations, which removes their files, and retry. Only that suffix
  counts: a finalized marker stays for the life of the store, so a bound on every name would
  eventually stop every finalize on a long-lived host. System teardown's existing 4096 bound
  counted every name and could stop teardown the same way; it now counts only `.request` names.
- The `.request` conflict check compares the whole module operation, as `teardown_preparation`
  does, rather than only the nonce. Finalize no longer records a nonce that teardown would refuse.
- A `.request` file that does not parse, and a marker without teardown refs once the terminal
  record is gone, now say what to do next. For the request: keep a copy, confirm no module volume
  of the activation's System and run remains in the pool, remove the file, then retry finalize.
  Removing it without that check would let finalize record no nonce, and teardown's
  unfinished-preparation proof would pass without looking at the pool. For the marker: confirm
  the activation's module volumes are gone from the pool, remove the marker, then retry teardown.
  Without the marker, teardown reads the live records it did before finalize.
- Crash-window tests cover the index removed with the record present (the retry finds the receipt
  through the marker) and the record removed with the index present (the retry finds it through
  the index). Each test fails if the index-then-record order or that retry path changes.
- The reviews described `refuse_finalized` followed by a write as relying on a single executor.
  That description is wrong: the module executor runs up to four calls at once. The
  check-then-act is ordered by the authority service's one active mutation per System lane
  (ADR-0584), and the `refuse_finalized` docstring now says so. A second authority instance
  writing the same store directory would break it.

Considered & rejected:

- **Scan `.object-index` files by content when the record is gone.** judgment: object indexes are
  removed before the index and the record, so a retry that finds the record gone has already
  removed them. The scan would add work and find nothing in that state.
- **Add a store lock around `refuse_finalized` and the write.** judgment: the System lane already
  serializes those calls within one service. A lock would only help a second process writing the
  same store directory, which this change does not introduce.
