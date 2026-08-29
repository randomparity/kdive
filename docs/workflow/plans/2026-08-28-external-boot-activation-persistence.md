# External-Boot Activation Persistence Implementation Plan

## Objective

Implement issue #2116's durable persistence slice from the approved design and accepted ADR-0583,
ADR-0584, and ADR-0585. The result is migration 0121, closed domain records and transitions, and
System-locked compare-and-set repository operations with durable reservation, recovery, terminal,
release, teardown, and cleanup evidence.

## Global constraints

- Touch only migration `0121`, `src/kdive/domain/`, `src/kdive/db/`, `tests/domain/`, `tests/db/`,
  and these issue-specific workflow documents.
- Do not add jobs, MCP tools, provider implementations, provider-native values, generation
  allocation, credential authentication, or a new ADR.
- Reuse `ExternalBootMaterialization`, `RecoveryPoint`, `OpaqueProviderRef`, the existing
  `IllegalTransition` taxonomy, and `advisory_xact_lock`.
- Every write predicates on System id, activation id, operation owner id, positive authority
  generation, expected state, and `cleanup_complete=false` where it changes lifecycle truth.
- Every repository write acquires `LockScope.SYSTEM` inside the caller's open transaction. Recovery
  store capacity operations additionally use one deterministic store-scoped advisory lock without
  adding an independently callable runtime surface.
- Persist typed values as validated `jsonb`; reject noncanonical byte input before SQL and
  revalidate semantic values on load.
- First executable implementation action is a failing focused test. Commit each green task and
  commit review-driven fixes separately.

## Task 1: Domain lifecycle and canonical evidence

Files:

- Modify `src/kdive/domain/capacity/state.py`.
- Add `src/kdive/domain/external_boot_activation.py`.
- Add `tests/domain/test_external_boot_activation.py`.

Red:

1. Add exhaustive tests for every same-enum pair of `ExternalBootActivationState` and
   `ExternalBootReservationState`, including a property test proving the legal adjacency table and
   every illegal edge.
2. Add tests for strict/frozen activation, reservation, release, and recovery-attempt records.
3. Add evidence tests for all six schema literals and identity prefixes, closed fields, UTC
   timestamps, canonical UUID/digest/reference leaves, duplicate-free canonical object ordering,
   byte bounds, noncanonical-byte rejection, and cross-record identity binding.
4. Pin the approved pre-recovery canonical JSON and
   `sha256:76aa7c43e0423a3dbf594c556dccbac8b98aed727d7e1978b47a96486015ad35`.
5. Run `just test-verbose tests/domain/test_external_boot_activation.py` and observe failure.

Green:

1. Add `ExternalBootActivationState` and `ExternalBootReservationState` to the nested transition
   table and expose them beside the existing lifecycle enums.
2. Implement one private closed canonical-value base in the new domain module, using compact sorted
   UTF-8 JSON, the 65,536-byte bound, and identity
   `sha256(prefix + b"\0" + canonical_json)`.
3. Implement `ExternalBootConflictEvidenceV1`, `ExternalBootPreRecoveryEvidenceV1`,
   `ExternalBootTerminalEvidenceV1`, `ExternalBootReleaseEvidenceV1`,
   `ExternalBootTeardownEvidenceV1`, and `ExternalBootCleanupEvidenceV1` with the exact approved
   literals, variable-leaf validation, canonical object ordering, and cross-field rules.
4. Implement strict row models for activation, live reservation, immutable release tombstone, and
   recovery attempt. Model validators enforce row-local state/deadline/evidence/cleanup invariants;
   cross-row rules remain repository responsibilities.
5. Rerun the focused test, then `just lint` and `just type`; refactor only after green.

## Task 2: Migration 0121 and database invariants

Files:

- Add `src/kdive/db/schema/0121_external_boot_activations.sql`.
- Add `tests/db/test_external_boot_activation_migration.py`.

Red:

1. Add migration tests that assert the four tables, trigger-maintained timestamps, supporting
   `runs(id, system_id)` uniqueness, composite Run/System foreign key, exact state/deadline/cleanup
   checks, JSON byte caps, immutable owner/store/byte identities, attempt identity and ordering,
   immutable release tombstones, and the ADR-0583 partial uniqueness predicate.
2. Add positive and negative rows for every state-matrix branch, including the released-but-not-
   cleaned interruption state and both teardown-cleanable states.
3. Add runtime-role privilege tests: `kdive_server` owns the stated mutation surface, worker and
   reconciler are read-only, and release tombstones cannot be updated or deleted.
4. Run `just test-verbose tests/db/test_external_boot_activation_migration.py` and observe failure.

Green:

1. Add the supporting unique key on `runs(id, system_id)` without changing Run semantics.
2. Create `external_boot_activations`, `external_boot_reservations`,
   `external_boot_reservation_releases`, and `external_boot_recovery_attempts` with named checks and
   foreign keys. Add update timestamps only to mutable tables.
3. Add the one-active-activation partial unique index: all preparing through conflict/failed rows
   remain included, while recovered/abandoned rows leave the index only after cleanup.
4. Add immutable-column and immutable-tombstone triggers where PostgreSQL constraints alone cannot
   protect an identity from direct mutation.
5. Apply explicit table grants and revoke default mutation access from worker/reconciler roles.
6. Rerun the focused migration tests, then `just lint` and `just type`.

## Task 3: System-locked creation, reads, and stale-generation CAS

Files:

- Add `src/kdive/db/external_boot_activations.py`.
- Add `tests/db/test_external_boot_activation_repository.py`.

Red:

1. Add fixtures that create a Resource, Allocation, System, Investigation, and Run with exact
   Run/System binding, then construct validated activation and reservation values.
2. Test atomic idempotent creation, current/reservation/attempt reads, bounded descending attempt
   pagination, and collision rejection for another active activation on the same System.
3. Test `record_materialization`, `record_pre_recovery_evidence`, ordinary transitions, and direct
   conflict persistence from preparing/prepared/activating/active.
4. For each mutator, test wrong System, activation, owner, generation, expected state, and cleaned
   state. Snapshot all four tables before a stale-generation call and prove every row/evidence value
   remains byte-for-byte unchanged afterward.
5. Run the focused repository test and observe failure.

Green:

1. Implement tagged `applied | superseded | not_found` CAS results without revealing which
   authority component mismatched.
2. Implement create and read operations with typed `jsonb` conversion and revalidation.
3. Implement one System-lock helper and use it in every mutator; keep the SQL CAS predicate
   load-bearing after lock acquisition.
4. Implement idempotent evidence fills and transition writes, rejecting illegal domain edges before
   SQL. Direct conflict atomically creates a conflict-attempt row and advances the activation.
5. Rerun focused tests, `just lint`, and `just type`; simplify repeated SQL only after the third
   occurrence.

## Task 4: Capacity, recovery attempts, terminalization, and cleanup

Files:

- Modify `src/kdive/db/external_boot_activations.py`.
- Extend `tests/db/test_external_boot_activation_repository.py`.

Red:

1. Test pending-to-ready admission at exact cap, rejection over cap, idempotent retry, and two-
   System concurrent admission serialized by one store identity.
2. Test immutable activation deadlines and per-attempt recovery deadlines, ordinary recovery,
   two distinct conflict-resolution attempts, mandatory resolution operation/idempotency identity/
   acknowledged composite state, and both terminal results from a pre-recovery basis without a
   fabricated `RecoveryPoint`.
3. Test typed release validation for known-object subset, zero-object abandonment, interrupted
   partial publication, exact store/owner/byte binding, missing absence proof, and byte-identical
   retry.
4. Test the interruption boundary after release but before cleanup completion; then idempotently
   finish cleanup.
5. Test authorized teardown from `recovery_failed` and `recovery_conflict`: the System must be
   durably `torn_down`, release capacity, retain all prior evidence, and reject every later
   lifecycle transition. Test missing/corrupt ownership evidence retains capacity.
6. Run the focused repository test and observe failure.

Green:

1. Implement store-scoped locked capacity summation and pending-to-ready admission.
2. Implement recovery-attempt creation/finish with monotonic attempt numbers, immutable basis and
   deadlines, and current-attempt identity fencing.
3. Implement release as one transaction that validates typed evidence, deletes the live debit, and
   inserts/returns the immutable tombstone.
4. Implement cleanup completion under the System lock, including the released-cleanup-pending
   intermediate state and teardown-specific System/evidence predicates.
5. Rerun focused domain/database tests, `just lint`, and `just type`; refactor after green.

## Task 5: Verification and handoff preparation

Files:

- Modify only the files above for defensible test or review fixes.

Steps:

1. Verify new tests bite with controlled faults to one transition edge, the authority-generation
   SQL predicate, and the post-cleanup fence; observe red and revert each fault.
2. Run `just test-verbose tests/domain/test_external_boot_activation.py`.
3. Run `just test-verbose tests/db/test_external_boot_activation_migration.py`.
4. Run `just test-verbose tests/db/test_external_boot_activation_repository.py`.
5. Run `just lint`, `just type`, and `just ci` bare.
6. Run whole-branch adversarial review, the diff-scoped security review, and the simplification
   pass. Fix defensible findings in separate commits and repeat affected guardrails.
7. Push the branch, create the issue-linked PR using a body file checked by `just check-pr-body`,
   wait for required checks, and stop only at a green mergeable PR with the exact `MERGE-READY`
   trajectory handshake. Do not merge.
