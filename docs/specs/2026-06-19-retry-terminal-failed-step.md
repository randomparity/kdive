# Retry a terminally-failed install/boot step without a rebuild (#603)

- **Issue:** #603 — Transient install/boot step failure wedges the Run (no retry without a rebuild)
- **ADR:** [ADR-0185](../adr/0185-retry-terminal-failed-step.md)
- **Status:** Accepted

## Problem

`runs.install` / `runs.boot` enqueue their step job with a dedup key `<run_id>:<step>`
(`mcp/tools/lifecycle/runs/steps.py::_enqueue_step`). `queue.enqueue` is upsert-then-fetch
(`INSERT … ON CONFLICT (dedup_key) DO NOTHING` then `SELECT`), so once a step job exists it returns
that **same** job in whatever terminal state it reached — including `failed` at
`attempt == max_attempts`. There is no re-enqueue path. A transient, retryable blip (a paused domain
— cf. #602 — a guest-agent reconnect, a TLS hiccup) therefore wedges the Run permanently: the only
recovery is a new Run, which rebuilds the kernel from scratch (tens of minutes on a remote host).

Observed live on D2 (2026-06-19): `runs.install` failed once with a transient `transport_failure`;
retrying returned the same failed job `eb4ab713…` (attempt 3/3) immediately. Recovery required raw
SQL (`DELETE FROM jobs WHERE dedup_key='<run>:install' AND state='failed'`).

A failed install/boot step abandons its `run_steps` row (`runs_install.py:78` /
`runs_boot.py:258`) and leaves the Run `SUCCEEDED` (only a *build* failure fails the Run). So
`runs.get` already reports the step as `pending` and suggests `runs.install` — but calling it just
returns the old failed job. The suggestion is a dead end; `transport_failure` is even marked
`retryable: true` on the envelope, with no way to act on it.

## Goal / success criteria

1. After an install/boot step job fails terminally, calling `runs.install` / `runs.boot` again
   enqueues a **fresh** attempt (the same job row reset to `queued`, `attempt = 0`) and the worker
   runs it — with no new build and no manual DB edit.
2. An **in-flight** (`queued` or `running`) step job is still deduped: a double call does not
   enqueue twice and does not disturb a running worker's lease.
3. A terminally `succeeded` step job is not resurrected by a re-call.
4. The recycle is scoped to install/boot steps. Every other dedup-keyed job (`provision`, `build`,
   `teardown`, `reprovision`, `power`, `force_crash`, `capture_vmcore`, image build) keeps today's
   behavior — in particular a failed `provision` job stays `failed` so admission can surface its
   original redacted reason via `get_by_dedup_key` (ADR-0149).
5. `runs.get` next-action stays consistent: it already reports the abandoned step as `pending` and
   suggests `runs.install` / `runs.boot`; after this change that suggestion actually re-runs the
   step. No change to `runs.get`.
6. The recycled re-run claims its step cleanly even if the prior failed step's `run_steps` row was
   **not** removed. `abandon_run_step` is best-effort (`runs_common.py:15-20` logs and swallows), so
   a stale `running` row can linger. `claim_run_step` (`db/idempotency.py:114-144`) already tolerates
   this: it reaps a `running` row older than `_STALE_RUNNING_INTERVAL` and otherwise waits-then-
   reclaims, so a recycled job whose owner is gone self-heals (worst case a bounded wait), never a
   hard `UNIQUE(run_id, step)` conflict. The fix relies on this existing behavior — it adds no new
   `run_steps` handling.

Falsifiable check: a queue test that fails a step-keyed job to `failed` at `attempt == max_attempts`,
then `enqueue(..., retry_terminal_failed=True)`, asserts the returned job is `queued` with
`attempt == 0` and cleared `worker_id`/lease/`error_category`/`failure_context` and is now
claimable by `dequeue`; plus a test that the same call with an in-flight (`queued`/`running`) or
`succeeded` job leaves it untouched, and that the default (`retry_terminal_failed=False`) leaves a
failed job untouched.

## Approach

Make terminal-failed recycle an **opt-in** on `queue.enqueue`, applied only by the step admission
path:

- `queue.enqueue` gains `retry_terminal_failed: bool = False`. When `True`, inside the same
  transaction and after the `INSERT … ON CONFLICT DO NOTHING`, run one fenced statement:

  ```sql
  UPDATE jobs
     SET state = 'queued', attempt = 0, worker_id = NULL, lease_expires_at = NULL,
         heartbeat_at = NULL, error_category = NULL, failure_context = '{}'::jsonb,
         result_ref = NULL
   WHERE dedup_key = %s AND state = 'failed'
  ```

  The `AND state = 'failed'` fence means a freshly-inserted `queued` row, an in-flight
  `queued`/`running` row, or a `succeeded`/`canceled` row is never touched — so in-flight dedup
  (criterion 2) and no-resurrect (criterion 3) hold for free. `attempt = 0` is required: without it
  the recycled job still sits at `max_attempts` and `dequeue` would never claim it (the same wedge).
  Then `SELECT … WHERE dedup_key` returns the job as today.

- `_enqueue_step` (the sole install/boot enqueue site, JobKind INSTALL/BOOT) passes
  `retry_terminal_failed=True`. It already holds the per-Run advisory lock
  (`advisory_xact_lock(conn, LockScope.RUN, run.id)`), which serializes concurrent retries on the
  same Run so the reset cannot race itself; the `state = 'failed'` fence is the belt-and-suspenders.

- No other `enqueue` caller passes the flag, so their behavior is unchanged.

The recycle is a **queue-internal re-admission** expressed in raw SQL, consistent with how
`queue.py` already manages job state (worker-fenced raw `UPDATE`s, not the `can_transition`
adjacency). Jobs are never driven through `can_transition` (that guards the `runs`/`systems`
repositories only), so `JobState.FAILED` stays terminal in `domain/capacity/state.py` and the
state-adjacency test is unchanged.

## Non-goals

- No per-attempt dedup key (`<run>:install:<n>`): it leaves a growing trail of dead failed jobs and
  changes the natural key other code reads. Rejected in the ADR.
- No new `ops.job_requeue` tool: `runs.install` / `runs.boot` are the existing explicit operator
  affordance; recycling on re-call achieves the retry without new tool surface.
- No gating the recycle on the failure's `error_category` (retryable vs deterministic). Each
  `runs.install` call is one explicit operator action and one fresh `max_attempts` cycle; a
  deterministic failure simply re-fails with the same error, which is bounded and self-evident — no
  infinite loop. Adding retryability classification is extra surface the issue did not ask for.
- No change to `run_steps`, `runs.get`, the worker, or any DDL — no migration.

## Risks

- Recycling resets `failure_context`/`error_category`, so the *previous* failed attempt's reason is
  not preserved on the job row after a retry. This is acceptable for install/boot (the operator just
  saw the failure envelope and chose to retry) and does not affect the provision path, which keeps
  its failed job intact (flag defaults off). Noted in the ADR.
- A second `enqueue` caller that later wants recycle must pass the flag explicitly; the default-off
  keeps the blast radius to install/boot. A test pins that the provision/build dedup keys are
  unaffected by the new parameter's default.
