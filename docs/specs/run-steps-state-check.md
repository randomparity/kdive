# Spec — CHECK constraint and parity coverage for `run_steps.state`

Tracks issue #562. Decision record: [ADR-0171](../adr/0171-run-steps-state-check.md).

## Problem

`run_steps.state` (`src/kdive/db/idempotency.py`) is a two-value service-local state
machine (`running`, `succeeded`, the `_RunStepState` enum) backing the idempotency
ledger, but the DB column is bare `text NOT NULL` with no `CHECK`
(`src/kdive/db/schema/0001_init.sql`). A row holding any other value makes
`claim_run_step` poll forever: the loop returns only on `succeeded`, treats `running`
as "another caller owns the claim, wait", and an unknown value falls through that
same wait path. The unknown value is also not stale-`running`, so the stale-claim
`DELETE` never clears it. This is unlike `debug_sessions.state` and every durable
lifecycle column, which carry an enum-pinned `CHECK`.

## Goals

- The DB enforces the `run_steps.state` state machine, matching the standard set by
  the durable lifecycle tables.
- `claim_run_step` fails fast with a clear invariant error on an unknown state instead
  of polling forever.
- Schema tests keep the Python enum and the SQL CHECK in exact, bidirectional sync.

## Non-goals

- Changing the idempotency contract, the `running`/`succeeded` semantics, or any tool
  / worker surface.
- Reworking the stale-claim reclamation or the concurrency model.
- Promoting `_RunStepState` to a public type.

## Design

See ADR-0171 for the decisions and rejected alternatives. In summary:

1. **Migration `0043_run_steps_state_check.sql`** — forward-only, validating:
   `ALTER TABLE run_steps ADD CONSTRAINT run_steps_state_check
   CHECK (state IN ('running', 'succeeded'))`.

2. **`claim_run_step` read-path guard** — after the `succeeded` replay branch, treat
   `running` as the only remaining valid value (wait and retry); any other value
   raises `RuntimeError` naming the offending value and the expected set, aborting the
   loop. The guard sits inside the existing transaction/cursor block where the row is
   read.

3. **Parity tests** —
   - Register `("run_steps_state_check", _RunStepState)` in
     `test_migrate.py::CHECK_ENUMS` (the enum-⊆-SQL guard shared by every lifecycle
     table).
   - Add a dedicated test asserting the CHECK's admitted literal set *equals*
     `{s.value for s in _RunStepState}` — the SQL-⊆-enum direction, catching an
     SQL-only extra value that `CHECK_ENUMS` cannot.

## Acceptance criteria

- [ ] `run_steps.state` has a SQL `CHECK` admitting exactly `running` and `succeeded`.
- [ ] A schema test verifies the Python enum values and the SQL CHECK values are in
  exact sync, including rejecting an SQL-only extra value.
- [ ] `claim_run_step` raises a clear invariant `RuntimeError` on an unknown persisted
  state rather than polling forever.
- [ ] Tests cover a stale `running` row (reclaimed), a `succeeded` replay, and an
  invalid-state row (rejected by the CHECK on insert, and the read-path guard raising
  when such a row is present).

## Test plan

- **Migration:** a fresh migrate admits a `running` and a `succeeded` row and rejects
  a third value with `CheckViolation`; the bidirectional parity test; the
  `CHECK_ENUMS` parametrization picks up the new entry.
- **`succeeded` replay:** a `claim_run_step` against a step whose row is `succeeded`
  returns `StepClaim(claimed=False, result=...)` with the stored result.
- **Stale `running` reclamation:** stage a `running` row older than
  `_STALE_RUNNING_INTERVAL` (30 minutes), then assert `claim_run_step` deletes it and
  re-claims (`claimed=True`). The `run_steps_set_updated_at` trigger rewrites
  `updated_at := now()` on every row-changing UPDATE, so a plain backdating UPDATE
  cannot age the row. Age it one of two ways: INSERT the row with an explicit
  `updated_at = now() - interval '31 minutes'` (the `BEFORE UPDATE` trigger does not
  fire on INSERT), or `ALTER TABLE run_steps DISABLE TRIGGER run_steps_set_updated_at`
  around the aging UPDATE and re-enable it after (the established
  `tests/reconciler/test_orphaned_active_sweep.py::_age_updated_at` pattern). The
  reclamation assertion (`claimed=True`) is what fails if the row was not actually
  aged, so the staleness is not silently lost.
- **Invalid-state read-path guard:** `claim_run_step` raises `RuntimeError` (not a
  hang) when a row with an unknown state is present. `claim_run_step` reads the state
  straight from the DB with no injection seam, so the only way to stage corrupt data
  is to remove the CHECK: on a fresh per-test migrated database, `ALTER TABLE
  run_steps DROP CONSTRAINT run_steps_state_check`, insert a row with an out-of-enum
  state, then assert `pytest.raises(RuntimeError)` *escapes* `claim_run_step` (proving
  the poll loop aborts rather than rolling back and retrying). The constraint drop
  runs on the test's own freshly-migrated DB so it does not leak into other tests.
