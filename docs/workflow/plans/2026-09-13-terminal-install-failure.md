# Restore terminal install failure Run compensation

## Goal and architecture

Restore the shared `succeeded -> failed` Run edge and the worker's matching compensation guard.
Worker finalization already holds the Run lock, finalizes the job, and writes Run failure fields in
one transaction; `runs.get` already maps a failed Run to the failure envelope.

## Tech stack and constraints

Python 3.14, psycopg, pytest, and `just`. Preserve ADR-0179, the worker fence and lock order, and
ADR-0185 retry recycling. Add no migration, public response shape, tool parameter, retry-policy,
or boot behavior.

Expected implementation size: 25–50 changed lines (M) — two production edits plus focused state,
worker, and read-path assertions.

## Task — Restore and prove the terminal transition

Files: `src/kdive/domain/capacity/state.py`, `src/kdive/jobs/worker.py`,
`tests/domain/test_state.py`, `tests/jobs/test_worker.py`, and
`tests/mcp/lifecycle/test_runs_tools.py` if the existing worker test cannot reach `runs.get`.

Interfaces: consume `RunState`, `can_transition`, `_fail_job_and_run`, and existing lifecycle
fixtures. Produce the legal `succeeded -> failed` edge and terminal compensation while retaining
the `JobState.FAILED` and worker-fence guards.

Verification:

- Mode: focused-test. Contract: `can_transition(RunState.SUCCEEDED, RunState.FAILED)` is true.
  Expected red: the current assertion is false. Green: `just test-verbose tests/domain/test_state.py`
  exits 0.
- Mode: focused-test. Contract: terminal install failure writes `failed`, category, and failing job;
  a requeue keeps the Run `succeeded`. Expected red: terminal result remains `succeeded`. Green:
  `just test-verbose tests/jobs/test_worker.py` exits 0.
- Mode: focused-test. Contract: `runs.get` returns the existing failure envelope after terminal
  finalization. Expected red: the response is successful. Green:
  `just test-verbose tests/mcp/lifecycle/test_runs_tools.py` exits 0.

Steps:

1. Add `RunState.FAILED` to `RunState.SUCCEEDED` in both the production table and its exhaustive
   test oracle.
2. Add `RunState.SUCCEEDED` to `_RUN_COMPENSATION_STATES`; retain all other guards unchanged.
3. Add the terminal/retryable worker proof and, only if absent there, an MCP read-path proof.
4. Run the focused commands, then `just lint`, `just type`, `just test`, and `just ci` before
   handoff.

Acceptance: shared state and worker agree, terminal failure is actionable via the existing
envelope, and retryable/fence-miss behavior does not regress. Rollback is a code revert only.
