# 0787 — Abortable before-finalize proof checkpoint

## Status

Proposed

## Context

`ops.resolve_recovery_orphan` acts on `external_boot_recovery_quarantine` rows. Only the
authority writes one. It does so when cleanup finalization fails after the terminal journal
record: `_publish_cleanup_quarantine` re-reads the private receipt and publishes it through the
head-fenced SQL function ([ADR-0615](0615-authority-owned-orphan-disposition.md)). On a healthy
host that failure does not happen. The [ADR-0622](0622-native-authority-fault-proof-barrier.md)
barrier pauses only around the provider commit. A restart there recovers through
`_finish_recovery` and quarantines nothing. Issue #3185 needs a real quarantined orphan for live
proof, and it forbids synthetic database writes and unreviewed proof-control expansion.

## Decision

We will extend the ADR-0622 barrier with one checkpoint and one action:

- **`before-finalize`.** `_finalize_adapter` awaits it inside its existing `try`, immediately
  before `adapter.finalize`. That places it on exactly the path where a failure publishes a
  quarantine.
- **`abort`.** It is valid only while the single arm is `reached` at `before-finalize`. It fails
  that one wait with `ProofBarrierAbort` and clears the arm.

`_provider_error` classifies the abort as `provider_conflict`. The authority then runs its own
publication fences unchanged. Every other ADR-0622 property holds:

- the socket is root-only by `SO_PEERCRED`;
- only one arm exists at a time;
- requests are closed JSON;
- the listener selects no provider, path or command;
- shutdown aborts a wait and never releases it.

## Consequences

- The local installed authority can produce one real quarantine row on demand, bound to an exact
  System, Run and operation.
- The release job's next attempt finalizes normally and removes the receipt, so the orphan the
  resolver sees is already absent at the provider.
- The remote authority has no finalizer, so the checkpoint never fires there.
- An abort costs no service restart and none of the unit's start budget.
- `checkpoint()` now clears its arm in a `finally`. A shutdown-aborted wait therefore leaves no
  stale arm in a process that keeps running.

## Considered & rejected

- **Record the local orphan as blocked too.** judgment: this leaves the obligation unprovable on
  every lane when a bounded proof seam reaches the real publisher.
- **Pause at `before-finalize` and restart the authority.** verified: both authority unit
  templates set `StartLimitBurst=5` within `StartLimitIntervalSec=600`
  (`deploy/ansible/roles/*/templates/*authority*.service.j2` at `b2d93172e`). In addition, the
  worker's attempt would end on transport loss rather than a bound `provider-conflict`.
- **Perturb the private tombstone directory so `finalize_tombstone` raises.** judgment: it couples
  the proof to private layout. The same entry would also break the resolver's own delete.
- **Seed quarantine rows with SQL.** verified: migration 0144 lets only `kdive_provider_authority`
  execute `publish_external_boot_recovery_quarantine_authority`. A seeded row also proves no
  authority publication.
- **Keep the arm armed across retries so the receipt survives to resolution.** judgment: under
  ADR-0714 the second identical `provider-conflict` ends the release job. Recovering that job is
  #3143's path, which is excluded from #3185.
- **Saturate the four-slot provider offload pool.** judgment: it depends on timing luck, which
  ADR-0622 rejected.
