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
- **Read path:** `claim_run_step` raises `RuntimeError` (not a hang) when a row with
  an unknown state is present — exercised by inserting the row with the constraint
  temporarily dropped (the only way to stage corrupt data), or by driving the branch
  directly. A `succeeded` row replays its result; a stale `running` row (older than
  the stale interval) is reclaimed and re-claimable.
