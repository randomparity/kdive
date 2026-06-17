# Spec — `runs.cancel`: free a System by canceling a non-terminal Run

- **Issue:** [#535](https://github.com/randomparity/kdive/issues/535)
- **ADR:** [ADR-0158](../adr/0158-runs-cancel-tool.md)
- **Date:** 2026-06-17
- **Status:** Proposed

## Problem

A non-terminal Run (`created`/`running`) holds its System and blocks every new
`runs.create` on that System with `transport_conflict: system_has_live_run`
(`src/kdive/mcp/tools/lifecycle/runs/create.py` `_preconditions_block_response`,
`common.py` `RUN_NON_TERMINAL`). The only existing escape is `systems.teardown`, which
cascades into release + re-request + re-provision after a reconciler grace delay
(one-System-per-Allocation, ADR-0149). A one-field profile mistake therefore costs a full
environment reset.

The state machine already permits `CREATED → CANCELED` and `RUNNING → CANCELED`
(`src/kdive/domain/state.py`); only a tool that drives the transition is missing.

## Goal

Add `runs.cancel(run_id)`: a single MCP call that drives a non-terminal Run to terminal
`canceled` under the per-Run lock, best-effort cancels its in-flight build job, and frees
the System immediately for a new Run — no teardown, no grace delay, no Allocation
re-request.

## Non-goals

- No new Run state, no state-machine edge change, no schema migration.
- No `runs.delete` / `runs.abort` verb — `canceled` is the existing terminal state.
- No hard worker interrupt; build-job cancellation is the existing cooperative
  `JobState → CANCELED` signal.
- No change to `runs.create`'s `system_has_live_run` gate — once the Run is `canceled` it
  is terminal and no longer counted by `RUN_NON_TERMINAL`, so the gate clears on its own.

## Behavior

`cancel_run(pool, ctx, run_id)` — a plain async handler in
`src/kdive/mcp/tools/lifecycle/runs/cancel.py`, wrapped by a `runs.cancel` FastMCP tool in
`registrar.py` (no provider resolver; `_docmeta.mutating()`,
`meta={"maturity": "implemented"}`).

1. **Bad `run_id` (not a UUID)** → `configuration_error` (`config_error`).
2. **Unknown / cross-project `run_id`** (no row, or `run.project not in ctx.projects`) →
   `not_found`. An ungranted-project Run looks absent; no existence oracle.
3. **Caller lacks `operator`** on the Run's project → `require_role` raises
   (`RoleDenied`/`AuthorizationError`), the established mutating-tool behavior.
4. **`created` or `running` Run** → under `conn.transaction()` +
   `advisory_xact_lock(conn, LockScope.RUN, run.id)`:
   - `RUNS.update_state(conn, run.id, RunState.CANCELED)` (legal edge).
   - best-effort: `job = queue.get_by_dedup_key(conn, f"{run.id}:build")`; if `job` is
     non-terminal (`queued`/`running`), `JOBS.update_state(conn, job.id,
     JobState.CANCELED)`. A missing or already-terminal job is not an error.
   - audit `runs.cancel`, `transition=f"{prior}->canceled"`.
   - → **success** envelope `status="canceled"`,
     `suggested_next_actions=["runs.create"]`, `data={"project": run.project}`.
5. **Already `canceled`** → **success** no-op: `status="canceled"`,
   `suggested_next_actions=["runs.create"]`, `data={"project": run.project}`. Idempotent;
   no audit row (nothing transitioned).
6. **`succeeded` or `failed`** → **`conflict`** failure with
   `data={"current_status": <state>}`. The Run already reached a different terminal outcome;
   it is not relabeled. The System is already free, so this is informational.

Cases 5–6 are reached by catching `IllegalTransition` from `update_state` and re-reading the
Run's state under the same lock to disambiguate (already-`canceled` vs other-terminal).
`RUNS.update_state` opens its own inner `conn.transaction()` (a savepoint), so the
`IllegalTransition` rolls back only that savepoint; the outer cancel transaction and its
held `LockScope.RUN` survive, and the disambiguating re-read is a fresh `RUNS.get` on the
same connection after the savepoint unwinds. The `conflict` envelope's
`data["current_status"]` is populated from that re-read — a test asserts it is present (not
empty), which proves the post-`IllegalTransition` read actually executed rather than the
catch merely firing.

## Concurrency

The whole operation runs under `advisory_xact_lock(conn, LockScope.RUN, run.id)` — the same
per-Run lock that `runs.build` (`build.py:148`), the worker's success path
(`finalize_build`, `runs_shared.py:21`), and the worker's failure path (`_fail_build`,
`jobs/handlers/runs.py:59`) all take. That shared lock is the load-bearing safety property
and must not be removed or narrowed:

- **Cancel races a worker mid-build.** If the worker holds the lock first, cancel blocks
  until the worker's `running->succeeded`/`running->failed` transaction commits, then reads
  the now-terminal state and returns the idempotent (`canceled`) or `conflict`
  (`succeeded`/`failed`) envelope. If cancel holds the lock first, it sets the Run
  `canceled`; the worker then observes the non-`RUNNING` state on its own `FOR UPDATE`
  re-read and no-ops — `finalize_build` returns early when the Run is not `RUNNING`
  (`runs_shared.py:30`), and `_fail_build` catches the resulting `IllegalTransition`
  ("a concurrent cancel won", `jobs/handlers/runs.py:82-88`). Neither path resurrects a
  canceled Run.
- **Why the build-job cancel is atomic with the Run transition.** Both happen inside the one
  locked transaction, so a `runs.build` concurrently enqueuing a fresh build job cannot
  leave an uncanceled job behind a `canceled` Run: the enqueue serializes on the same lock.

An implementer must not "simplify" away either worker guard (`finalize_build`'s
`RunState.RUNNING` re-check or `_fail_build`'s `IllegalTransition` catch) — they are what
makes the cooperative build-job cancel safe.

## Success criteria (falsifiable)

Each is a test in `tests/mcp/lifecycle/test_runs_tools.py` (handler called directly with an
injected pool + `ctx`, the file's existing pattern):

1. **Cancel a `created` Run** → envelope `status == "canceled"`; the `runs` row is
   `canceled`; an audit row with `transition == "created->canceled"` exists.
2. **Cancel a `running` Run** → envelope `status == "canceled"`; row `canceled`; audit
   `transition == "running->canceled"`.
3. **Idempotent no-op on an already-`canceled` Run** → envelope `status == "canceled"`,
   `error_category is None`; no second audit row beyond the one that set it `canceled`.
4. **Cancel frees the System** → seed a `created` Run on a `ready` System whose Investigation
   is open; `runs.create` a second Run on that System fails `system_has_live_run`; after
   `runs.cancel` the first Run, a fresh `runs.create` on the same System **succeeds**.
5. **Best-effort build-job cancellation** → seed a `running` Run, enqueue its build job
   (`f"{run_id}:build"`, `queued`); after `runs.cancel`, that job's state is `canceled` and
   the Run is `canceled`. A second variant: an already-`succeeded` build job is left
   untouched and the cancel still succeeds (best-effort, no error). A third variant: the
   build job is `running` (a worker holds its lease) — `runs.cancel` still drives the Run to
   `canceled` and the job to `canceled`, and a subsequent `runs.create` on the same System
   succeeds.
5a. **A worker that finishes after a cancel does not resurrect the Run** → seed a `running`
   Run, `runs.cancel` it, then invoke `finalize_build` (the worker success path) on that
   Run; the Run stays `canceled` (`finalize_build` no-ops on a non-`RUNNING` Run,
   `runs_shared.py:30`), it is not driven back to `succeeded`.
6. **Unknown `run_id`** (valid UUID, no row) → `not_found`. **Malformed `run_id`** →
   `configuration_error`.
7. **`succeeded`/`failed` Run** → `conflict` with `data["current_status"]` equal to the
   actual terminal state **and present/non-empty** (proving the post-`IllegalTransition`
   re-read ran); the row is unchanged (still `succeeded`/`failed`).
8. **Authz** → a `viewer`-only ctx on the Run's project raises on cancel (no mutation); a
   ctx whose `projects` omits the Run's project gets `not_found`.

## Guardrails

`just ci` (lint, type, … , test) green before every commit. New handler ≤100
lines/function, cyclomatic ≤8, absolute imports, Google-style docstrings on the public
handler, 100-char lines, and plain factual prose per the repo doc-style convention.
