# Queue admission ownership

## Problem

MCP replay lookup and step replay markers duplicate the queue's terminal recycle policy.
The definitions currently agree, but future policy changes require coordinated edits.

## Scope

Implement one ownership correction under [ADR-0858](../../adr/0858-queue-recycle-policy-owner.md).
Move the four-policy replay lookup from MCP `_idempotency.py` to `jobs/queue.py`.
One five-policy terminal-state map drives both SQL eligibility parameters and replay lookup.
The clock-dependent policy retains FAILED terminal eligibility and its existing separate
RUNNING database-clock predicate; predictive lookup rejects it before any database access.
Keep SQL text, transaction, attempt fencing, payload, authorization and audit unchanged.
Retain MCP's canonical import re-export for existing callers and their pre-admission ordering;
remove its policy map and implementation, with no forwarding wrapper.
In `runs/steps.py`, remove the terminal map and prior-row prediction. `_locked_enqueue`
uses existing `enqueue_with_status` and returns `not admitted` as its replay marker.
Preserve ledger policy and queued/running lookup; fix stale `_step_recycle` caller guidance.
Keep replay-before-external-boot-guard guidance with the moved helper.

### Failure model

- Actors: MCP callers retry steps while workers update durable jobs in Postgres.
- Invariants: no new work on replay; clock-dependent leases are evaluated only by SQL;
  external-boot guards retain their order, and audit remains in the caller transaction.
- Accept: predictive replay describes the observed row, not a guarantee against concurrent
  mutation. The actual admission result determines the post-enqueue replay marker.
- Exclude new locking, migration, public response, schema or authorization behavior.

## Success

The queue is the sole terminal eligibility owner. Both replay entrypoints agree with real
admission for all four independent policies and absent/queued/running/failed/succeeded/canceled
rows. Clock-dependent prediction fails before DB access. Existing boot/install replay,
force/re-stage and external-boot admission behavior remains covered unchanged.

## Validation

- focused-test: `tests/jobs/test_queue.py::test_dedup_replay_matches_admission` exercises the
  24-state/policy matrix through queue and MCP exports. Expected RED: neutral queue API absent;
  existing MCP cases preserve baseline. GREEN: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest
  tests/jobs/test_queue.py tests/mcp/tools/test_idempotency.py -q`.
- focused-test: existing clock-policy rejection test covers both exports without private-map
  assertions; absent queue API is structural RED. Deliberate eligibility regression must fail.
- focused-test: preservation GREEN: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest
  tests/mcp/lifecycle/test_runs_tools.py tests/services/external_boot/test_reverse_admission.py -q`.
  Existing queue cases cover lapsed exhausted/live leases, attempts and canceled opt-in.
- focused-test: SQL literal/transaction and resolved-signature comparisons; run `just lint`,
  whole-tree `just type` and hooks. Review all removed policy/prediction paths and callers.
- task-test-not-applicable: docstring corrections need no prose snapshot; no live VM/provider
  operation changes. Real Postgres regressions are the applicable end-to-end proof.
