# Cleanup quarantine and tombstone finalization run under the pinned session (#3012)

## Scope and authority

Campaign scope for issue #3012, token `q3012-2a97756c`. Operator-approved exclusions
(2026-09-30): the ADR-0586 proof shape and the #2140 authority journal; re-adding artifact-parent
creation on session open; read-path behavior changes. Decision record:
[ADR-0710](../../adr/0710-local-external-boot-session-opens-its-artifact-root-on-first-use.md)
amendment of 2026-09-30. ADR-0586 needs no change: it does not say whether finalization opens a
session.

## Problem

`LocalLibvirtExternalBoot.record_cleanup_quarantine` and `finalize_cleanup_tombstone`
(`lifecycle/boot/external_boot.py`) call `self._io.record_cleanup_quarantine` and
`self._io.finalize_tombstone` without an operation session, and do not use `authority`. #2898
made them session-free because a session open re-created the pruned activation parents. Since
ADR-0710 a session open creates nothing. These two cleanup-completion writes can therefore move
under the lease's ownership snapshot. The recovery-object writes in `delete_recovery_object` and
`adopt_object` are also unpinned and stay out of this change (Failure model, entry 4).

## Replay after lease or domain loss

This was settled before any code was written.

- **Lease.** `LocalExternalBootAuthorityAdapter._offload` wraps every port call in
  `LocalOperationLeaseScope.issue(authority, binding)`, so `resolve` cannot fail inside a call. A
  restart replay is a new call and gets a new lease. No replay depends on a lease that is gone.
- **Session open** (`LocalExternalBootSessionFactory.open`: lane pin, libvirt connect, owned
  domain lookup and XML ownership, overlay open) is the precondition this change adds. On the
  first execution of an operation, each production caller has just opened a session on the same
  binding, with the authority's System lane active (`service.py` `lane.active`), so no other
  mutation of that System runs in between:
  1. `_apply` (DELETE): `cleanup_is_accounted` and `cleanup` open sessions, then
     `record_cleanup_quarantine` runs.
  2. `_prepare_system_teardown_recovery`: `cleanup_is_accounted` opens a session before
     `finalize_cleanup_tombstone`. When cleanup is not yet accounted, `recover` and `cleanup` open
     sessions before its `record_cleanup_quarantine`. All of this runs before `teardown_system`
     destroys the domain. `teardown_system` runs only after absence is proven, and a replay that
     finds absence returns before reaching either write.
  3. `finalize`: the point comes from `cleanup_receipt` (opens a session), or from the pending map
     that `_apply` filled, after which `_observe` ran `cleanup_is_accounted` (opens a session).
- **Terminal replay** holds `lane.lock` but not `lane.active`. The pending map is in-process and
  can carry a point across requests: a TERMINAL anchor that fails after its durable write skips
  `finalize` and leaves the entry behind. If a System teardown then finalizes the same tombstone
  and destroys the domain, a later terminal replay of the DELETE pops that stale point. Today the
  session-free write takes ADR-0586's absent branch and succeeds. Pinned, the session open fails
  and the replay ends in `provider_conflict`. This is the only replay this change turns from
  success into failure. After any authority restart the same state already fails this way,
  because the replay goes through `cleanup_receipt`. Failure model class 1 accepts it.
- **Post-delete replay of an absent tombstone** (ADR-0586) keeps its store branch. Finalization
  never touches the domain or overlay, so while the System exists a replay opens its session in
  the same way.

Conclusion: make the full change. A narrower "pin only when resolvable" form guards no reachable
state; the ADR-0710 amendment records that rejection with its evidence.

## Design

1. In both ports, keep the existing proof/point check first (a malformed proof opens no session).
   Then run `with self._io.open(authority, _expected_binding(recovery.binding)):` around the
   unchanged `self._io` store call. Replace the session-free comments with one line citing
   ADR-0710's amendment.
2. No change to `RealLocalExternalBootIO`, `RecoveryMetadataStore`, the authority adapter, the
   `LocalExternalBootIO` protocol, or any persisted record.

## Success

- S1: each port resolves the lease once and opens one session with the binding's expected
  ownership, makes its store write while the session is open, and closes the session once.
- S2: if lease resolution or session open fails, the port writes nothing. The quarantine receipt
  stays absent, or the tombstone stays present.
- S3: a malformed proof is rejected before any lease resolution.
- S4: with the production session factory (`LocalExternalBootSessionFactory`,
  `LocalOperationLane`, `LocalOperationLeaseScope`, `LocalArtifactRoot`; only libvirt faked),
  quarantine, then finalization, then a second finalization (ADR-0586 replay) leave the recovery
  root empty and `exact_recovery_absence` true.

## Failure model

1. **Actors and deployments**
   - The local external-boot authority on a local-libvirt worker host, which runs these ports
     inside `_offload` lease scopes.
2. **Invariants and assets at stake**
   - Exact recovery absence stays provable after cleanup (System teardown depends on it).
   - Finalization stays idempotent for ADR-0586's absent-tombstone replay.
   - Recovery-root writes made by activation ports run under the pinned ownership snapshot.
3. **Accepted failure classes**
   - A missing owned domain or overlay at the write makes it fail as `provider_conflict` and
     leaves the operation unresolved. This covers an out-of-band removal between the caller's
     preceding session and this one, and the stale-pending terminal replay after a System
     teardown described above. Accepted because the same state already fails that way through
     the preceding port, or through `cleanup_receipt` after any authority restart. Pinning the
     write to an owned domain is the point of the change.
   - Session close failing after a durable write raises after the write. Both writes are
     idempotent on retry: quarantine replaces identical bytes, and finalization takes the absent
     branch.
4. **Covered elsewhere**
   - Proof authentication and journal ordering: ADR-0586 / #2140 (operator).
   - `delete_recovery_object` and `adopt_object` also write after `observe_object`'s session has
     closed. They are outside this charter and reported as a follow-up candidate.
