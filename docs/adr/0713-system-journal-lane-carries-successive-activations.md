# 0713 — One System journal lane carries successive activations

## Status

Accepted (2026-09-30)

Amends [ADR-0584](0584-provider-host-authority-fences-external-boot-mutations.md): it states that
a System can host a new external-boot activation after its prior activation completed release, and
it limits predecessor adoption to the records of one activation.

## Context

The authority journal is one lane per System and authority instance. The database head has the
primary key `(authority_instance, system_id)` (migration 0123), and the host keeps one file per
System (`<system_id>.jsonl`). Every activation, release, and teardown on the System appends to that
lane, and each new authority gets the next System generation.

Two facts already admit a new activation after a completed release:

- `advance_external_boot_authority_journal_head` (migration 0123) accepts a `watermark-installed`
  record after a `terminal` head that has no pending takeover.
- `check_external_boot_admission` (`src/kdive/services/external_boot/admission.py`) does not
  restrict a System whose activation is cleaned and terminal, because
  `get_restricting_for_system` excludes it.

But the #2865 native proof got `authority: journal-conflict` on every attempt of a second
activation on a System after a completed release (#2952). A connected reproduction found the
failed check. `ExternalBootAuthorityService.execute_mutation` looks for a predecessor before it
admits a preparation operation (`materialize`, `prepare`) or a release phase (`recover`,
`cleanup`). The search took the latest `terminal` record with the same operation and a lower
generation, from any activation on the lane. On the second activation it found the first
activation's record, `_operation_matches` failed on `activation_id`, and the service refused with
`predecessor_operation_mismatch` (`release_phase_mismatch` for a second release). Each retry
found the same record again, so every attempt failed in the same way.

Predecessor adoption exists for a takeover: a later generation of the same activation, or of the
same release, adopts the terminal result that an earlier generation recorded. A record of another
activation is history, not a predecessor.

## Decision

1. **Reuse is supported.** A System can host a new external-boot activation after its prior
   activation completed release (`cleanup_complete`). The new activation continues the same
   journal lane. Core admission does not refuse it.
2. **Adoption is scoped to one activation.** The predecessor search for a preparation operation
   and for a release phase considers only `terminal` records whose `activation_id` equals the
   request's `activation_id`. A matching record of the same activation that fails
   `_operation_matches` still refuses with `journal_conflict`.
3. **The carrier keeps both fixture modes.** `fixture_mode` `create` and `verify-existing` are
   both valid on a System with a prior completed activation. The runbook documents the modes and
   this rule.

## Consequences

- A second activation and release on one System pass the journal checks. The connected test
  `tests/integration/test_external_boot_system_reuse.py` proves activate, release, activate,
  release on one System.
- A takeover of the same activation adopts its predecessor as before.
- An activation that is not cleaned and terminal still restricts the System under ADR-0583, so a
  second activation cannot start while the first one holds the System.
- The old refusal came before the `admitted` record, so it wrote no journal record and set no
  lane state. The journal file and the database head stay equal. An authority that runs the old
  code refuses every reuse; only an upgrade to this change removes the refusal, and a restart
  does not.

## Considered & rejected

- **Refuse a second activation in core admission.** verified: the journal head SQL (migration
  0123) and the admission table (`admission.py`, cleaned terminal rows are excluded) already admit
  it. A refusal would remove designed behavior to hide a scoping defect, and the operator would
  need a new System for each carrier run.
- **Start a new journal lane per activation.** judgment: the lane orders every mutation on one
  System. Separate lanes would lose the order between a release and the next activation, and would
  need a schema change to the head's primary key.
- **Scope by `run_id` or `plan_identity` instead of `activation_id`.** judgment: one activation
  has one Run and one plan, so the result is the same. `activation_id` names the unit that owns
  a takeover.
