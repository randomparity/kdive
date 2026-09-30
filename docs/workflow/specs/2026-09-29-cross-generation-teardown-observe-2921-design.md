# Cross-generation System teardown observation (#2921) — design

Decision record: [ADR 0620](../../adr/0620-authority-owned-system-teardown.md), amendment
2026-09-29 (#2921).

## Problem

A System's retained teardown record is keyed by activation binding, not by authority generation.
`begin_system_teardown` (local `RecoveryMetadataStore`, remote
`RemoteModuleVolumePreparationStore`) lets a
later generation of the same subject overwrite it in place. Observation of generation N rebuilds
N's anchor from N's `mutation-started` record and requires the retained intent to match it
exactly (`matches_anchor`, generation included). When the record holds another generation's
intent, local `observe_system_teardown` raises `ValueError("System teardown observation conflicts
with retained intent")` and remote `reopen_system_teardown` raises the remote equivalent. The
service maps that to `provider_conflict` in recovery (`_finish_recovery` → `_provider_error`) and
lets it escape unmapped from `_execute_teardown`'s post-mutation re-observation.

Two shapes produce the mismatch:

1. **Successor-owned record.** A later generation M > N began and overwrote the record before
   N was observed.
2. **Predecessor-owned record.** N anchored `mutation-started` but stopped before its `begin`
   wrote the record, so the record still holds an earlier generation's intent.

## Reachability (evidence)

- **Shape 2 is reachable in takeover recovery.** `execute_system_teardown` (both providers)
  writes the record inside the provider call, after the service anchors `mutation-started`
  (`service.py` `execute_mutation`). A process death between the two leaves N's
  `mutation-started` unresolved over generation N−1's record. So does a failed
  `_resolve_confirmed` recheck after `mutation-started`, which answers `superseded` before the
  provider is called, with no crash. Every later takeover recovers N
  (`_acknowledge_takeover_bound` → `_recover_suspended` → `_finish_recovery` →
  `_recovery_observation`), gets `provider_conflict`, and never reaches
  `takeover-acknowledged`, so the System can never be torn down.
- **Shape 1 is not reachable in takeover recovery.** A generation M begins only from
  `execute_mutation`, which requires M's own `takeover-acknowledged` record; the takeover writes
  that record only after `_recover` and the unresolved-phase check find every operation phase
  resolved (`_acknowledge_takeover_bound`, second lane-lock block). The journal is one file per
  System and the head fences it (`_recover`), so while N is unresolved no M > N can have begun.
  A recovery that meets a successor-owned record therefore indicates a head/journal
  inconsistency outside this contract; it gets the same bounded answer as below (a guard, not a
  new path), covered by a service test with a fake adapter.
- **Shape 1 is reachable in `_execute_teardown`.** `execute_mutation`'s `run()` sets
  `active.done` in its `finally`, before `_execute_teardown` re-observes N's facts outside the
  lane lock. A concurrent takeover by M (ADR-0584's overlap, `active.done.wait()`) can acknowledge
  and begin M, rewriting the record, before N's re-observation reads it. N is superseded; today it
  gets an unmapped `ValueError`.

## Design

1. `src/kdive/providers/external_boot_authority/teardown.py` adds
   `class SystemTeardownSupersededError(Exception)` — the retained record belongs to a later
   generation of the same teardown subject.
2. Each provider intent (`LocalSystemTeardownIntentV1`, `RemoteSystemTeardownIntentV1`) adds
   `anchor_subject_matches(anchor) -> bool`, comparing `binding`, `plan_identity`,
   `provider_kind`, `authority_instance`: `same_subject` minus `reservation`, which the anchor
   does not carry. `authority_id`, `operation_digest` and the journal fields change per
   generation; `operation_identity` and `attempt_id` are fixed per activation but, as in
   `begin`'s successor rule, are not part of the subject (`binding` already names the
   activation).
3. Observation classifies a retained record that does not `matches_anchor(anchor)`:
   - different subject, or same generation → unchanged `ValueError` (→ `provider_conflict`);
   - retained generation > anchor generation → raise `SystemTeardownSupersededError`;
   - retained generation < anchor generation → treat the record as absent for this anchor.
   Local: in `RealLocalExternalBootIO.observe_system_teardown`, the predecessor case sets
   `retained = None`, so facts are anchor-owned with `reservation=None` and `completed_at=None`.
   Remote: `RemoteModuleVolumePreparationStore.reopen_system_teardown` returns `None` for the
   predecessor (documented on the method; its only caller is the adapter's observation),
   so `observe_system_teardown` builds anchor-owned facts with `domain_validated=False`.
4. `service.py` `_system_teardown_facts` maps `SystemTeardownSupersededError` to
   `AuthorityServiceError("superseded")`. `_finish_recovery` already re-raises an
   `AuthorityServiceError` unchanged, and `_execute_teardown` propagates it, so both call sites
   get `superseded` from the one mapping.
5. ADR 0620 gains a dated amendment recording the rule and its rejected alternatives.

No persisted format, migration, journal phase, protocol field or transport category changes.

## Failure model

1. **Actors and deployments** — the external-boot authority service (local and remote libvirt
   providers) recovering or re-observing a System teardown; a concurrent takeover by a later
   generation of the same activation's teardown; process death between `mutation-started` and
   the provider `begin`.
2. **Invariants and assets at stake** — observation is read-only (never creates, starts,
   resumes or destroys a domain, never writes the record); the teardown reservation credits
   exactly once; a foreign record (different subject) still refuses; recovery of a stuck
   `mutation-started` converges.
3. **Credit** — a predecessor-owned observation carries `reservation=None`, so `teardown_proof`
   yields `retained_quarantine` and N can never credit. A superseded observation yields no facts
   and no proof for N; the successor that overwrote the record credits under its own exact
   anchor (its `begin` adopted the record, keeping phase and `completed_at`).
4. **Accepted failure classes** — a successor-owned record in takeover recovery (unreachable
   above) returns `superseded` on every takeover; it needs operator reconciliation, like an
   unreconcilable `journal_conflict`, and is a bounded answer, not a retry path. A record whose subject fields match
   but whose reservation differs is classified by generation, not refused: `begin` never writes
   such a record, and both outcomes (quarantine without credit, `superseded`) fail closed.
5. **Covered elsewhere** — exhausted retained requeue (#2917), generation churn (#2901), payload
   residue (#2920), tombstone crash window (#2927), non-System partial abort (#2926), periodic
   journal check (#2933).

## Success

- Local and remote: observing N over an N−1 record returns anchor-owned facts
  (`intent_identity == anchor.identity`, `reservation is None`, `completed_at is None`, not
  complete) and writes nothing; over an N+1 record raises `SystemTeardownSupersededError` and
  writes nothing; over a different-subject record still raises `ValueError`.
- Service: takeover recovery of a `mutation-started` teardown head whose adapter reports a
  predecessor-owned record anchors `terminal` with a `retained_quarantine` observation and
  acknowledges the takeover; an adapter raising `SystemTeardownSupersededError` yields
  `superseded`, not `provider_conflict`.

## Validation

Focused tests, each shown red under a controlled fault:

- `tests/providers/local_libvirt/test_external_boot.py` and
  `tests/providers/remote_libvirt/test_external_boot_authority.py`: predecessor-owned,
  successor-owned, and another subject (different plan, different activation, same generation
  with a different anchor field) at retained generations N−1, N and N+1.
- `tests/providers/external_boot_authority/test_service_teardown.py`: the service over the real
  remote adapter and store recovers a generation that died before `begin` through the next
  takeover (red on the unmodified provider with `provider_conflict`); a post-commit
  re-observation under a successor record answers `superseded`. The shared fake repository in
  `service_support.py` gains a per-generation acknowledgement lookup and keeps a `None`
  source/target identity as `None`, which multi-generation teardown recovery needs.

Then `just lint`, `just type`, `just records`, `just ci`.
