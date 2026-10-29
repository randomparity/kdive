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
`mutation-started` record, not the retry's, and the remote adapter's exact comparison with the
receipt holds. The receipt is not rewritten, so the published quarantine row and the object port's
digest fences keep naming what the first attempt recorded.
