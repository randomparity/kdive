# ADR 0171 — Database-enforce the `run_steps.state` machine and fail fast on impossible state

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers

## Context

`run_steps` is the idempotency ledger (`src/kdive/db/idempotency.py`, ADR-0005,
ADR-0016). Its `state` column is a service-local state machine with exactly two
values, defined by the private `_RunStepState` enum: `running` (a claim is in
flight) and `succeeded` (the step's result is recorded). `claim_run_step` reads the
row and branches on it: `succeeded` replays the stored result, an absent row claims
the step, and a `running` row means another caller owns the claim so the caller
waits and retries.

Two gaps let an impossible value cause an unbounded hang:

- **No SQL `CHECK`.** `src/kdive/db/schema/0001_init.sql` creates
  `run_steps.state text NOT NULL` with no constraint, unlike the sibling
  `debug_sessions.state`, which carries `CHECK (state IN ('attach', 'live',
  'detached'))`, and unlike every durable lifecycle / category column whose CHECK is
  pinned to its Python enum by `tests/db/test_migrate.py::CHECK_ENUMS`. The column
  accepts any text.
- **No read-path guard.** In `claim_run_step` the only explicit branch is
  `state == 'succeeded'`. Every other value — including a value the enum does not
  define — falls through to the `running`-means-wait path: `asyncio.sleep` then
  retry. An unknown state is also not stale-`running`, so the stale-claim `DELETE`
  never clears it. The loop spins forever.

Adding the CHECK is the load-bearing fix, but a CHECK only rejects *future* bad
writes; it does nothing for a row that predates the constraint or that arrives by a
path outside the ORM (manual SQL, restore, replication skew). The read path should
refuse to spin on a value it cannot interpret.

## Decision

1. **Add a validating SQL `CHECK` to `run_steps.state`.** Forward-only migration
   `0043_run_steps_state_check.sql` (ADR-0015):
   `ALTER TABLE run_steps ADD CONSTRAINT run_steps_state_check CHECK (state IN
   ('running', 'succeeded'))`. The two admitted values mirror `_RunStepState`
   exactly.

2. **Fail fast in `claim_run_step` on an unknown persisted state.** After the
   `succeeded` replay branch, treat `running` as the only other valid value (wait and
   retry). Any other value raises `RuntimeError` with the offending value and the
   expected set, aborting the poll loop instead of sleeping forever.

3. **Pin the constraint to the enum bidirectionally.** Register
   `run_steps_state_check` in `test_migrate.py::CHECK_ENUMS` (the project-wide
   enum-⊆-SQL guard every lifecycle table uses), and add a dedicated test asserting
   the CHECK's admitted set *equals* the enum's value set — closing the direction
   `CHECK_ENUMS` structurally cannot check: a value present in SQL but absent from the
   enum (an SQL-only extra).

## Consequences

- A corrupt or out-of-enum `run_steps.state` can no longer be inserted, and if one
  ever exists it surfaces as a `RuntimeError` naming the value rather than an
  unbounded hang in `claim_run_step`.
- The `RuntimeError` matches the module's existing "can't happen" ledger invariants
  (`run_step`'s post-`ON CONFLICT` missing-row check, `complete_run_step`'s
  not-running check), so the failure mode is consistent within the module.
- Adding or removing a `_RunStepState` value now requires updating the migration; the
  two parity tests fail otherwise, in both directions.
- No change to the response envelope, the worker handlers, or any tool surface: the
  guard fires only on data that the state machine's own writers can never produce.

## Considered & rejected

- **`ADD CONSTRAINT ... NOT VALID` (skip validating existing rows).** The only writers
  of `run_steps.state` are `run_step` / `claim_run_step` / `complete_run_step`, which
  only ever write `running` or `succeeded`; no code path has ever written another
  value, so a validating add cannot fail on real data. A plain validating `ADD
  CONSTRAINT` matches every other CHECK migration in the tree (e.g.
  `0033_allocation_failure_category.sql`). `NOT VALID` would add ceremony for a
  validation that cannot fail.
- **Raise `IllegalTransition` instead of `RuntimeError`.** `IllegalTransition`
  (`domain/capacity/state.py`) is raised by the repository layer when
  `can_transition` rejects a *domain* state-machine edge. `run_steps` is a DB-layer
  idempotency ledger, not a domain object, and an unknown persisted value is corrupt
  data rather than an attempted illegal edge. `RuntimeError` is what this module
  already raises for its other ledger invariants; reusing it keeps the contract local
  and consistent.
- **Rely on the SQL `CHECK` alone and leave the read path unguarded.** A CHECK does
  not protect against a row that predates it or arrives outside the application
  writers; leaving the loop able to spin on an unreadable value keeps the original
  hang reachable. Defense-in-depth at the read boundary costs one branch.
- **Promote `_RunStepState` to a public `RunStepState`.** The enum is a genuine
  DB-layer implementation detail with no consumer outside `idempotency.py`; the parity
  tests import it within the package. Exposing it publicly only to satisfy a test is
  premature surface.
