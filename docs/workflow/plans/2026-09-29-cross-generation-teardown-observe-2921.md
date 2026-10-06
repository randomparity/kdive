# Cross-generation System teardown observation (#2921) — plan

Spec: [2026-09-29-cross-generation-teardown-observe-2921-design.md](../specs/2026-09-29-cross-generation-teardown-observe-2921-design.md).
Decision: ADR 0620 amendment 2026-09-29 (#2921).

## Global Constraints

- No migration, journal phase, protocol field or transport category change.
- Observation stays read-only in both providers: no record write, no domain mutation.
- Each new test is shown red against the unmodified classification before it is kept.
- Guardrails: `just lint`, `just type`, `just records`, focused `just test-verbose <paths>`,
  pre-push `just ci`.

## File map

| File | Change |
| --- | --- |
| `src/kdive/providers/external_boot_authority/teardown.py` | `SystemTeardownSupersededError` |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | `anchor_subject_matches`; observe classification |
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | `anchor_subject_matches`; `reopen_system_teardown` classification |
| `src/kdive/providers/external_boot_authority/service.py` | `_system_teardown_facts` maps the superseded error |
| `docs/adr/0620-authority-owned-system-teardown.md` | dated amendment |
| `tests/providers/local_libvirt/test_external_boot.py` | predecessor, successor, foreign cases |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | predecessor, successor, foreign cases |
| `tests/providers/external_boot_authority/test_service_teardown.py` | takeover over a predecessor-owned record converges (real remote adapter); superseded mapping |
| `tests/providers/external_boot_authority/service_support.py` | fake repository: per-generation acknowledgement; `None` identities kept |

## Task 1 — shared signal and local classification

Contract: local `observe_system_teardown(anchor N)` over a retained N−1 record returns facts with
`intent_identity == anchor.identity`, `reservation is None`, `completed_at is None`; over N+1
raises `SystemTeardownSupersededError`; over a different `plan_identity` or the same generation
with other fields changed raises `ValueError("...conflicts...")`. The recovery root is
byte-identical before and after, and the session records no destroy/undefine.

Steps: write the three tests (replacing
`test_system_teardown_observation_does_not_adopt_a_successor_request`, whose read-only assertion
moves into the predecessor case); run red; add the error and `anchor_subject_matches`; classify in
`RealLocalExternalBootIO.observe_system_teardown`; run green.

## Task 2 — remote classification

Contract: the same three outcomes through `RemoteExternalBootAuthorityAdapter`'s
`observe_system_teardown` / the store's `reopen_system_teardown`, with the teardown file
byte-identical after each call.

Steps: tests red; implement in `reopen_system_teardown`; green.

## Task 3 — service mapping and takeover convergence

Contract: `_system_teardown_facts` turns `SystemTeardownSupersededError` into
`AuthorityServiceError("superseded")`, so neither recovery nor `_execute_teardown` reports
`provider_conflict` for it. Over the real remote adapter and store: generation 1 begins and
fails, generation 2 dies after `mutation-started` before `begin`, and generation 3's takeover
recovers 2 to a `terminal` `conflict` observation, then completes its own teardown with the only
`complete_ready` proof. A post-commit re-observation under a successor record answers
`superseded` with the terminal record already anchored.

Steps: tests red (unmodified remote classification gives `provider_conflict`; removing the
mapping gives an unmapped error); implement; green.

## Task 4 — ADR amendment and guardrails

Amend ADR 0620; `just records`; `just lint`; `just type`; focused tests; `just ci`.

## Rollback

Revert the branch; no persisted state changes shape.
