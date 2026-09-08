# 0013 — A `failed` System strands its mutation obligations permanently

## Status

Open
review-by: 2026-12-08

## Concern

A System in `failed` carrying an open mutation obligation holds that attempt's `source.ext4` and
`scratch.ext4` out of the module-volume reaper for the life of the row, and no path ever repairs it.

Three facts combine, each verified against the tree at the time of writing:

- `RemoteModuleAttemptObligationRepository.retained_owners`
  (`src/kdive/db/remote_module_attempt_obligations.py:531-561`) selects purely on the discharge
  columns — `mutation_discharged_at IS NULL` — and never on System state. A `failed` System's open
  obligation therefore keeps returning from the retained-owner set exactly as a `torn_down` one
  does, which is what holds its volumes back (ADR-0588).
- `repair_orphaned_systems` treats `FAILED` as terminal (`_ORPHANED_SYSTEM_TERMINAL_STATES`,
  `src/kdive/reconciler/repairs/systems.py`), so the reconciler never enqueues a teardown for such a
  System, and teardown is the path that would discharge the obligation.
- `apply_authority_system_teardown` refuses with `cleanup-required` while an undischarged obligation
  exists (`src/kdive/db/schema/0149_authority_owned_system_provisioning.sql:1151-1157`), so a
  `failed` System carrying one cannot reach `torn_down` either — it cannot become a candidate for
  the `torn_down` repair lane this record accompanies.

So the row is stranded in both directions: nothing discharges it, and it cannot transition into the
state whose repair would.

## Why deferred

Issue #2326 scopes its detection predicate to `s.state = 'torn_down'`, and the operator-approved
exclusion set for that issue does not include `failed`. Widening the lane's predicate to cover
`failed` is a different repair with a different safety argument: `torn_down` is terminal and its
teardown is the event that authorizes the discharge, whereas `failed` is reached from several paths
that have no teardown behind them, so "the System is gone, release the recovery point" is not
established the same way. That argument belongs to its own change, not to this one.

## Non-regression boundary

- ADR-0634's lane must not start discharging obligations for `failed` Systems as a side effect. Its
  candidate query pins `s.state = %s` bound to `SystemState.TORN_DOWN.value`, and
  `test_non_terminal_system_is_untouched` in
  `tests/reconciler/test_leaked_mutation_obligation_repair.py` holds that boundary — it goes red
  when the state predicate is removed.
- This record does not make the `failed` case worse. It is a pre-existing leak with the same cause
  as the `torn_down` one #2326 repairs; the lane neither reaches those rows nor changes how
  `retained_owners` reads them.

## What would resolve it

A repair that discharges mutation obligations for Systems in `failed`, or an argument recorded in an
ADR that such obligations must be retained rather than discharged. It is done when a `failed` System
carrying an open obligation either leaves the retained-owner set on its own, or the record explains
why it should not. The check is the same shape as the `torn_down` arms: seed a `failed` System with
an open obligation, run the reconciler, and assert the intended outcome.

## Provenance

target: src/kdive/db/remote_module_attempt_obligations.py
target: src/kdive/reconciler/repairs/systems.py
Found by the scope audit of the ADR-0634 design during issue #2326, 2026-09-08, and confirmed
against the three sources cited above. Deferral-record number assigned by the campaign orchestrator;
the tracker issue is filed separately by the campaign, not by this change.
