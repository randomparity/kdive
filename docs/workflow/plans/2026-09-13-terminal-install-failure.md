# Restore terminal install failure Run compensation

## Goal and architecture

Restore the shared `succeeded -> failed` Run edge and the worker's matching compensation guard.
Worker finalization already holds the Run lock and finalizes the job in one transaction; the
repository owns the matching guarded Run failure write. `runs.get` already maps a failed Run to
the failure envelope.

## Tech stack and constraints

Python 3.14, psycopg, pytest, and `just`. Preserve ADR-0179, the worker fence and lock order, and
ADR-0185 retry recycling. Add no migration, public response shape, tool parameter, retry-policy,
or boot behavior.

Expected implementation size: 80–140 changed lines (M) — an atomic guarded repository helper,
operation-specific worker eligibility, and focused state, terminal, retryable, boot-preservation,
and guard-rejection assertions.

## Task — Restore and prove the terminal transition

Files: `src/kdive/domain/capacity/state.py`, `src/kdive/db/repositories.py`,
`src/kdive/jobs/worker.py`, `tests/domain/test_state.py`, `tests/db/test_repositories.py`,
`tests/jobs/test_worker.py`, and
`tests/mcp/lifecycle/test_runs_tools.py` if the existing worker test cannot reach `runs.get`.

Interfaces: consume `RunState`, `can_transition`, `RUNS.record_terminal_failure`,
`_fail_job_and_run`, and existing lifecycle fixtures. Produce the legal `succeeded -> failed`
edge for terminal install compensation while retaining the `JobState.FAILED` and worker-fence
guards; preserve terminal boot's ADR-0230 read path.

Verification:

- Mode: focused-test. Contract: `can_transition(RunState.SUCCEEDED, RunState.FAILED)` is true.
  Expected red: the current assertion is false. Green: `just test-verbose tests/domain/test_state.py`
  exits 0.
- Mode: focused-test. Contract: a repository terminal failure write rejects an illegal Run source.
  Green: `just test-verbose tests/db/test_repositories.py::test_record_terminal_run_failure_rejects_an_illegal_source`
  exits 0.
- Mode: focused-test. Contract: terminal install failure writes `failed`, category, and failing job;
  a requeue keeps the Run `succeeded`. Expected red: terminal result remains `succeeded`. Green:
  `just test-verbose tests/jobs/test_worker.py` exits 0.
- Mode: focused-test. Contract: terminal boot failure preserves a build-succeeded Run for
  `boot_readiness`. Expected red: that Run becomes `failed`. Green:
  `just test-verbose tests/jobs/test_worker.py` exits 0.
- Mode: focused-test. Contract: a stale worker that loses its lease cannot apply the terminal
  Run transition. Green: `just test-verbose tests/adversarial/test_run_failure_atomicity.py::test_stale_worker_that_lost_its_lease_does_not_fail_the_run`
  exits 0.
- Mode: focused-test. Contract: `runs.get` returns the existing failure envelope after terminal
  finalization. Expected red: the response is successful. Green:
  `just test-verbose tests/mcp/lifecycle/test_runs_tools.py` exits 0.

Steps:

1. Add `RunState.FAILED` to `RunState.SUCCEEDED` in both the production table and its exhaustive
   test oracle.
2. Add a repository helper that locks, applies the central transition guard, and writes terminal
   failure metadata atomically; allow `RunState.SUCCEEDED` only for terminal install compensation.
3. Add terminal-install, terminal-boot-preservation, retryable worker, and repository-guard proof
   and, only if absent there, an MCP read-path proof.
4. Run the focused commands, then `just lint`, `just type`, `just test`, and `just ci` before
   handoff.

Acceptance: shared state and worker agree, terminal failure is actionable via the existing
envelope, and retryable/fence-miss behavior does not regress. Rollback is a code revert only.
