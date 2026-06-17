# ADR 0158 — `runs.cancel` drives a non-terminal Run to `canceled` without teardown

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** KDIVE maintainers

## Context

A non-terminal Run (`created` or `running`) holds its System until it reaches a terminal
state, and at most one non-terminal Run is allowed per System
(`src/kdive/mcp/tools/lifecycle/runs/common.py` — `RUN_NON_TERMINAL`). `runs.create`
enforces that single-non-terminal-Run-per-System gate
(`src/kdive/mcp/tools/lifecycle/runs/create.py` `_preconditions_block_response`): a second
`runs.create` while a Run is non-terminal returns
`transport_conflict: system_has_live_run`.

The runs tool surface (`registrar.py`) has six tools — `get`, `create`, `build`,
`complete_build`, `install`, `boot` — and **no cancel**. So a Run created with an
unbuildable profile (or stuck `running`) has no terminal exit short of
`systems.teardown`, which — via one-System-per-Allocation (ADR-0149) — cascades into
releasing and re-requesting the Allocation and re-provisioning a fresh System after the
reconciler's orphan-reaping grace delay. A cheap config mistake costs a full environment
reset. Found during black-box MCP evaluation
([#535](https://github.com/randomparity/kdive/issues/535)).

The relevant facts (confirmed against code):

- The state machine **already permits** the terminal transition
  (`src/kdive/domain/state.py`): `CREATED → {RUNNING, CANCELED}` and
  `RUNNING → {SUCCEEDED, FAILED, CANCELED}`. `CANCELED` is terminal (empty transition set).
  Only the tool that drives `* → CANCELED` is missing.
- `RUNS.update_state(conn, id, RunState.CANCELED)` (`src/kdive/db/repositories.py`) reads
  the current state `FOR UPDATE`, calls `ensure_transition`, and writes in one transaction.
  It raises `ObjectNotFound` (no row) or `IllegalTransition` (illegal edge — i.e. the Run is
  already terminal).
- A Run's build job has a deterministic dedup key `f"{run.id}:build"`
  (`build.py` `_enqueue_build`), reachable read-only via
  `queue.get_by_dedup_key(conn, dedup_key)` (ADR-0149, no new column). A job is canceled by
  `JOBS.update_state(conn, id, JobState.CANCELED)`; `JobState` permits
  `QUEUED → CANCELED` and `RUNNING → CANCELED` (`state.py`).
- The per-Run advisory lock is `advisory_xact_lock(conn, LockScope.RUN, run.id)`
  (`src/kdive/db/locks.py`), the same lock `runs.install` / `runs.boot` hold while
  enqueuing their step jobs (`steps.py`).
- Mutating runs tools require `Role.OPERATOR` (`require_role`, ADR/`rbac.py`); a Run in an
  ungranted project must look absent (`not_found`), never be acted on.
- The uniform envelope (`mcp/responses.py`) pairs `error_category` with a failure status
  iff the status is a failure; a terminal lifecycle state that is **not** itself a tool
  failure goes in `data`, not in `status` (the `jobs.cancel` precedent,
  `catalog/jobs.py`).

## Decision

Add a `runs.cancel(run_id)` MCP tool — a thin FastMCP wrapper over a plain async handler
`cancel_run(pool, ctx, run_id)` in a new `src/kdive/mcp/tools/lifecycle/runs/cancel.py` —
that, under the per-Run lock, drives a non-terminal Run to terminal `canceled`, best-effort
cancels its in-flight build job, and frees the System immediately. No teardown, no grace
delay, no Allocation re-request.

1. **Validate and authorize before mutating.** Parse `run_id` as a UUID (`config_error`
   on a malformed id). Read the Run; if it is absent or its project is not in
   `ctx.projects`, return `not_found` (an ungranted-project Run looks absent — no
   resource-existence leak). Then `require_role(ctx, run.project, Role.OPERATOR)`.

2. **Drive the transition under the per-Run lock.** Open `conn.transaction()` +
   `advisory_xact_lock(conn, LockScope.RUN, run.id)`, then call
   `RUNS.update_state(conn, run.id, RunState.CANCELED)`. The lock + the repository's
   `FOR UPDATE` read serialize a concurrent `runs.build` (`created → running`) or worker
   transition, so the cancel reads the authoritative current state and either applies the
   legal edge or raises `IllegalTransition`.

3. **Idempotent no-op on an already-`canceled` Run; conflict on `succeeded`/`failed`.**
   `IllegalTransition` from a terminal Run is disambiguated by re-reading the Run's state
   under the same lock:
   - **already `canceled`** → return a **success** envelope (`status="canceled"`). Cancel
     is idempotent: a retried cancel is a no-op the agent can act on, mirroring the issue's
     "idempotent no-op when already terminal".
   - **`succeeded` or `failed`** → return a **`conflict`** failure with
     `data["current_status"]` = the actual state. A Run that already reached a *different*
     terminal outcome must not be relabeled `canceled` — that would misrepresent a real
     build/boot result. The System is already free in both cases (the Run is terminal), so
     this is informational, not a dead end.

4. **Best-effort build-job cancellation, inside the same transaction.** After the Run
   transition succeeds, look up `queue.get_by_dedup_key(conn, f"{run.id}:build")`. If that
   job exists and is non-terminal (`queued`/`running`), call
   `JOBS.update_state(conn, job.id, JobState.CANCELED)`. "Best-effort" is literal: a missing
   job (never built) or an already-terminal job (build finished/failed) is **not** an error
   — the Run cancel still succeeds. The job-cancel is a cooperative signal; the worker's own
   terminal-state guard handles a job that completes in the race window. Only the Run-state
   transition is load-bearing for freeing the System.

5. **Audit the transition.** Record one `runs.cancel` audit event inside the same
   transaction, `transition` = `f"{prior}->canceled"` (`created->canceled` /
   `running->canceled`), so the audit log names the from-state. No audit row is written for
   the idempotent already-`canceled` no-op (nothing transitioned).

6. **Register without a resolver.** `runs.cancel` is a pure state mutation — it calls no
   provider runtime — so it registers like `runs.create`/`runs.install`/`runs.boot`
   (`_docmeta.mutating()`, `meta={"maturity": "implemented"}`), not via
   `with_runtime_for_run`.

7. **Success envelope.** Return `ToolResponse.success(str(run.id), "canceled",
   suggested_next_actions=["runs.create"], data={"project": ...})` — naming `runs.create`
   as the next action because the System is now free for a new Run, which is the whole point
   of the tool.

## Consequences

- **A stranded Run is recoverable in one call** — `runs.cancel(run_id)` frees the System
  immediately; the subsequent `runs.create` no longer hits `system_has_live_run`. No
  teardown, no reconciler grace delay, no `allocations.release`/`request`. Satisfies the
  acceptance criteria.
- **Cancel is idempotent** for the natural retry case (already `canceled` → success) but
  **does not relabel** a `succeeded`/`failed` Run (→ `conflict` with `current_status`), so a
  real outcome is never overwritten or hidden.
- **No migration, no new state, no state-machine change.** The `* → CANCELED` edges already
  exist; the build-job link is the existing deterministic dedup key.
- **No new race.** The per-Run lock + `update_state`'s `FOR UPDATE` serialize cancel against
  `runs.build`'s `created->running` flip and the worker's build transition. A cancel that
  loses the race to a worker that already drove the Run terminal reads that terminal state
  and returns the idempotent/conflict envelope; a cancel that wins blocks the worker's next
  transition attempt, which then reads `canceled` and stops (legal `running->canceled` is
  the only Run edge the worker could conflict on, and it is already terminal).
- **Best-effort job cancel cannot fail the operation.** A missing or already-terminal build
  job is expected (an un-built Run, or a build that finished in the window); the Run cancel
  is authoritative and the System is freed regardless. The worker that holds a leased build
  job observes the `canceled` job state cooperatively; it does not need a hard interrupt for
  correctness — the Run is already terminal, so its build result is discarded on the next
  guarded transition.
- **Authz is checked before any mutation** and an ungranted-project Run looks absent
  (`not_found`), so cancel introduces no resource-existence oracle and no cross-project
  cancel.

## Alternatives considered

- **Make `succeeded`/`failed` also idempotent no-ops (return success `canceled`).** Rejected:
  it would relabel a Run that actually built/booted (or failed) as `canceled`, destroying the
  distinction between "the agent abandoned this Run" and "this Run finished/failed". The
  System is already free in those states, so the agent loses nothing by getting a `conflict`
  that names the real `current_status`; it gains an honest record. Already-`canceled` stays a
  success because that *is* the cancel outcome.
- **Hard-kill / interrupt the in-flight build job (signal the worker).** Rejected: KDIVE jobs
  are cooperatively canceled (`jobs.cancel`, `JobState … → CANCELED`); there is no
  out-of-band worker-interrupt channel, and adding one is out of scope. The Run reaching
  `canceled` already makes any late build result a discarded transition, so cooperative
  cancel is sufficient to free the System.
- **Query the `jobs` table by `payload->>'run_id'` to catch every job kind.** Rejected:
  the only job that can be in-flight for a non-terminal Run pre-boot is the build job, and it
  has a deterministic dedup key already used by ADR-0149's `get_by_dedup_key`. A payload scan
  would be a non-indexed JSON query for no additional correctness; install/boot jobs only
  exist after the Run is `succeeded` (out of the cancellable set).
- **Add a separate `runs.delete` / `runs.abort`.** Rejected: the issue asks for one terminal
  exit; `canceled` is the existing terminal state for an abandoned Run, and the state machine
  already models it. A second verb would duplicate the transition with no distinct semantics.
- **Skip the per-Run lock (rely on `update_state`'s `FOR UPDATE` alone).** Rejected: the
  build-job lookup-and-cancel must be atomic with the Run transition so a `runs.build`
  enqueuing a job concurrently cannot leave a freshly-enqueued build job uncanceled after the
  Run is `canceled`. Holding `LockScope.RUN` (the same lock `runs.build` takes) closes that
  window; it is the established per-Run serialization point.
