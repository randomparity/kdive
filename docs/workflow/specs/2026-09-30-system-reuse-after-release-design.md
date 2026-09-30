# A System hosts a new activation after a completed release — design

Issue: #2968 (part of #2952). Scope token: q2968-0c0e756d. Decision: ADR-0713 (operator
decision A, support reuse).

## Problem

A second external-boot activation on a System whose first activation completed release gets
`authority: journal-conflict` with reason `predecessor_operation_mismatch`. The predecessor search
in `ExternalBootAuthorityService.execute_mutation`
(`src/kdive/providers/external_boot_authority/service.py`) takes the latest `terminal` record of
the same operation with a lower generation from the whole System lane. On a reused System it finds
the first activation's record, and `_operation_matches` fails on `activation_id`. The release-phase
search has the same defect (`release_phase_mismatch`). The refusal sets `lane.failed`, so the lane
refuses every later request until the authority restarts.

## Scope

1. **Service (criteria 1-2).** Add `record.activation_id == request.activation_id` to both
   predecessor searches: the preparation search (`materialize`, `prepare`) and the release-phase
   search (`recover`, `cleanup`). Nothing else changes.
2. **Connected regression (criterion 1).** New `tests/integration/test_external_boot_system_reuse.py`:
   public boot, public release, a second Run on the same System, public boot, public release,
   through a real `Worker`, the real authority service, `DatabaseAuthorityRepository`, and
   `FileAuthorityJournal`. Each job must reach `succeeded`.
3. **Unit guards (criteria 2-3).** In `tests/providers/external_boot_authority/test_service.py`:
   (a) a `materialize` for a new activation on a lane that has another activation's terminal
   `materialize` is admitted; (b) a successor `materialize` of the same activation whose Run and
   plan differ from its predecessor's still refuses with `journal_conflict` /
   `predecessor_operation_mismatch`.
4. **Record (criterion 4).** ADR-0713.
5. **Runbook (criterion 5).** Document `fixture_mode` and the reuse rule in both carrier sections
   of `docs/operating/runbooks/live-testing.md`. The carrier code needs no guard: both modes are
   valid on a reused System (ADR-0713 decision 3).

## Failure model

1. **Actors and deployments**: the fixed worker sends authority requests; the authority service
   on the provider host keeps one journal lane per System; the database holds the lane head.
2. **Invariants and assets at stake**:
   - a takeover generation adopts only its own activation's terminal predecessor;
   - a mismatching same-activation predecessor still refuses before any journal append;
   - the journal file and the database head stay equal.
3. **Accepted failure classes**:
   - An authority that already set a lane to failed under the old code needs a restart. The
     refusal wrote no record, so no repair is needed.
4. **Covered elsewhere**:
   - endless acknowledged retry: #2960 (ADR-0711);
   - carrier fixture cleanup: #2965;
   - the journal-conflict reason label: #2967.

## Success

- The connected test passes, and it fails with `predecessor_operation_mismatch` without the
  preparation scope and with `release_phase_mismatch` without the release scope.
- The unit test (b) passes before and after the change; the unit test (a) fails before it.

## Validation

- Service, `Mode: focused-test`: `tests/integration/test_external_boot_system_reuse.py` and the two
  unit tests. Bite: remove each scope line and observe red.
- Live, `Mode: live-proof`: on a native ppc64le (POWER9) KVM-HV host, run the ppc64le carrier twice
  on one System; the second run passes.
