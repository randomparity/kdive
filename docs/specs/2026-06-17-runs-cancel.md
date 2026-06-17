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
   untouched and the cancel still succeeds (best-effort, no error).
6. **Unknown `run_id`** (valid UUID, no row) → `not_found`. **Malformed `run_id`** →
   `configuration_error`.
7. **`succeeded`/`failed` Run** → `conflict` with `data["current_status"]` equal to the
   actual terminal state; the row is unchanged (still `succeeded`/`failed`).
8. **Authz** → a `viewer`-only ctx on the Run's project raises on cancel (no mutation); a
   ctx whose `projects` omits the Run's project gets `not_found`.

## Guardrails

`just ci` (lint, type, … , test) green before every commit. New handler ≤100
lines/function, cyclomatic ≤8, absolute imports, Google-style docstrings on the public
handler, 100-char lines, and plain factual prose per the repo doc-style convention.
