# Recovery-object delete and adopt write under the pinned session (#3045)

## Scope and authority

Campaign scope for issue #3045, token `q3045-5d4cfc13`. Operator-approved exclusions
(2026-09-30): changing `observe_object`, `record_cleanup_quarantine` or
`finalize_cleanup_tombstone`; other providers' recovery-object ports. Decision record:
[ADR-0710](../../adr/0710-local-external-boot-session-opens-its-artifact-root-on-first-use.md),
a second dated amendment. This follows #3012, whose design
([2026-09-30-session-pinned-cleanup-finalization-design.md](2026-09-30-session-pinned-cleanup-finalization-design.md),
Failure model entry 4) deferred these two ports.

## Problem

In `LocalLibvirtExternalBoot` (`lifecycle/boot/external_boot.py`), `delete_recovery_object` and
`adopt_object` call `observe_object`, which opens and closes a session around one read. They then
re-read the quarantine receipt and call `self._io.finalize_tombstone` or
`self._io.adopt_cleanup_quarantine` with no session open. These recovery-root writes do not run
under the lease's ownership snapshot.

## Replay analysis

This was settled before any code was written.

- **Callers.** The only production caller is `RecoveryOrphanAuthorityService.resolve_selected`
  (`external_boot_authority/orphan.py`). It runs under the mutation service's System `lane.lock`
  (`set_serializer`, `service.py` `_serialize_recovery_orphan`) and reaches the ports through
  `LocalExternalBootAuthorityAdapter.delete_recovery_object` / `adopt_recovery_object`.
  Each call runs inside one `_offload_recovery_object`, which wraps the whole port call in
  `LocalOperationLeaseScope.issue(authority, binding.binding)`. The lease cannot fail to resolve
  inside the call, and a restart replay is a new call with a new lease.
- **Session opens around the write.** Before either port is called, `resolve_selected` runs
  `observe_recovery_object`, which opens a session on the same binding. Inside the port,
  `observe_object` opens a session before the write and again after it (the return value). The
  port returns successfully only if that last open succeeds.
- **Can the domain or overlay already be gone?** Yes. A System teardown can finalize the tombstone
  and quarantine (`_prepare_system_teardown_recovery`) and destroy the domain while an orphan
  selection is still pending. A replay of that selection then fails at the service's first
  `observe_recovery_object`, as `provider_conflict`. It never reaches the port's write, with or
  without this change.
- **Does pinning turn a successful replay into `provider_conflict`?** No. The pinned open sits
  between two opens on the same binding, under the same lease and System lane, that the port
  already needs. If the domain, overlay, or libvirt connection is missing at the new open, it is
  also missing at the port's last open, and the call already fails today, after an unpinned write.
  The only state that changes outcome is a failure confined to the middle open: a libvirt
  connection or definition read that fails once and then recovers within one port call. Today the
  write lands and the port succeeds. Pinned, nothing is written and the call fails; the next
  disposition attempt observes the unchanged receipt and replays the write. Failure model class 1
  accepts it.
- **Idempotence.** A completed delete leaves the receipt absent, and a completed adopt leaves it
  managed. `resolve_selected` treats either as done and does not call the port again. A direct
  repeat call with the old digest fails the digest comparison before the write session opens.

Conclusion: make the change. A narrower "pin only when the session opens" form guards no reachable
state, for the same reason ADR-0710's first amendment gives.

## Design

1. In both ports, keep `observe_object` and its digest comparison first, unchanged. Then run the
   receipt re-read, its checks, and the store write inside
   `with self._io.open(authority, _expected_binding(binding.binding)):`. The final
   `observe_object` stays outside, after the session closes. Replace no other code.
2. No change to `observe_object`, `RealLocalExternalBootIO`, `RecoveryMetadataStore`, the
   `LocalExternalBootIO` protocol, the authority adapter, the orphan service, or any persisted
   record.

## Success

- S1: each port opens three sessions in order (observe, write, observe), each with the binding's
  expected ownership. The re-read and the store write run while the second session is open.
- S2: if lease resolution or session open fails for the write session, the port raises and writes
  nothing. The quarantine receipt stays present and unmanaged, and the tombstone stays present.
- S3: an observation digest mismatch raises before the write session opens.
- S4: with the production session factory (`LocalExternalBootSessionFactory`,
  `LocalOperationLane`, `LocalOperationLeaseScope`, `LocalArtifactRoot`; only libvirt faked) and
  one lease issued around each port call as the adapter does: a delete leaves the recovery root
  empty with `exact_recovery_absence` true, and a later `observe_object` under a new lease
  returns absent. An adopt leaves the receipt managed. Outside a lease scope, each port raises
  and writes nothing.

## Failure model

1. **Actors and deployments**
   - The local external-boot authority on a local-libvirt worker host, running recovery-orphan
     dispositions through `_offload_recovery_object`.
2. **Invariants and assets at stake**
   - A delete or adopt writes only under the pinned ownership snapshot for its binding.
   - A failed write session leaves the receipt exactly as observed, so the disposition replays.
   - Exact recovery absence after a delete (System teardown depends on it).
3. **Accepted failure classes**
   - A session open that fails only for the write session (libvirt connection or definition read
     failing once, then recovering within the call) makes the port fail closed without writing.
     Accepted: the receipt is unchanged and the next disposition attempt replays the write.
   - Session close failing after a durable write raises after the write. Accepted: the receipt is
     then absent or managed, which `resolve_selected` treats as done on retry.
4. **Covered elsewhere**
   - Folding the observation into the write session: changing `observe_object` is excluded
     (operator).
   - Remote-libvirt and fault-inject recovery-object ports: excluded (operator).
