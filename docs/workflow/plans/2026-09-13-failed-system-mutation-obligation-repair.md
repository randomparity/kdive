# Failed-System mutation-obligation repair plan

Goal: extend the existing reconciler repair so failed Systems no longer retain open remote-module
mutation obligations forever. The reconciler remains the only writer and uses the existing
SECURITY DEFINER discharge function under the existing System advisory lock. Tech stack: Python,
Postgres, pytest, and the repository's `just` recipes.

## Global Constraints

Keep the 100-candidate bound, stable order, teardown settle guard, per-candidate isolation, fixed
`terminal_escape` reason, and no new schema, privilege, provider, or MCP surface. ADR-0652 starts
Proposed and becomes Accepted only with this completed implementation.

Expected implementation size: 25–55 changed lines (M) — one candidate-query contract and focused
integration tests in the established repair module.

## File map

- `src/kdive/reconciler/repairs/systems.py`: terminal-state candidate predicate and explanatory
  contract.
- `tests/reconciler/test_leaked_mutation_obligation_repair.py`: failed-System repair coverage.
- `docs/adr/0652-repair-failed-system-mutation-obligations.md`: decision record.

## Task 1 — select failed terminal owners

### Verification

- Contract: an open obligation on a failed System is a candidate while a ready System is not.
  Mode: focused-test. Red: the new failed-System test returns zero against the current query.
  Green: `just test-verbose tests/reconciler/test_leaked_mutation_obligation_repair.py` passes.

### Interfaces

Consumes `SystemState` and `_LEAKED_MUTATION_CANDIDATES_SQL`; preserves
`repair_leaked_mutation_obligations(conn) -> int` for the repair catalog and callers.

1. Change the query state predicate to bind both `SystemState.TORN_DOWN.value` and
   `SystemState.FAILED.value` without changing ordering, limit, or teardown predicate.
2. Update the function docstring to state that it repairs both terminal states.
3. Add a failed-System test using the existing `_seed_open_obligation(..., system_state=FAILED)`
   helper; assert one discharge and `terminal_escape`.
4. Run the focused test command and expect all selected tests to pass.

## Task 2 — preserve existing safety behavior

### Verification

- Contract: a failed candidate with an active teardown stays open; a second pass is idempotent.
  Mode: focused-test. Red: removing the retained teardown predicate discharges the active-job row.
  Green: `just test-verbose tests/reconciler/test_leaked_mutation_obligation_repair.py` passes.

### Interfaces

Consumes the existing teardown-job seed helper and discharge read helper; preserves the same
candidate-count and logging contract as the torn-down lane.

1. Parameterize the active-teardown deferral test across `torn_down` and `failed` System states.
2. Parameterize the second-pass no-op test across those terminal states.
3. Run `just test-changed`, `just lint`, and `just type`; expect each command to pass.

## Completion

Advance ADR-0652 to `Accepted (2026-09-13)`, run `just ci`, and commit the implementation only
after all requested checks pass.
