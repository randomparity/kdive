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
