# ADR 0185 — Recycle a terminally-failed install/boot step job on re-enqueue

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers

## Context

`runs.install` / `runs.boot` enqueue a step job keyed `<run_id>:<step>`
(`mcp/tools/lifecycle/runs/steps.py::_enqueue_step`). `queue.enqueue` is upsert-then-fetch
(`INSERT … ON CONFLICT (dedup_key) DO NOTHING` then `SELECT`), so a re-issue returns the existing
job in whatever terminal state it reached. Once a step job dead-letters to `failed` at
`attempt == max_attempts`, every later `runs.install` / `runs.boot` returns that same failed job —
`dequeue` never reclaims it (it skips `failed`, and `attempt == max_attempts` fails the
`attempt < max_attempts` predicate). There is no supported re-enqueue.

A failed install/boot step abandons its `run_steps` row and leaves the Run `SUCCEEDED` (only a
*build* failure transitions the Run to `failed`). `runs.get` therefore reports the step as
`pending` and suggests `runs.install`, but calling it is a dead end. A transient, retryable blip
(a paused domain per #602, a guest-agent reconnect, a brief TLS drop) thus wedges the Run, and the
only recovery is a new Run that rebuilds the kernel from scratch — minutes wasted on a remote host.
Reproduced live on D2 (2026-06-19); recovery needed raw SQL to delete the failed job row (#603).

## Decision

Add an opt-in `retry_terminal_failed: bool = False` parameter to `queue.enqueue`. When set, after the
`INSERT … ON CONFLICT DO NOTHING` and within the same transaction, a single fenced statement resets
a terminally-failed row for that dedup key back to a fresh queued state:

```sql
UPDATE jobs
   SET state = 'queued', attempt = 0, worker_id = NULL, lease_expires_at = NULL,
       heartbeat_at = NULL, error_category = NULL, failure_context = '{}'::jsonb,
       result_ref = NULL
 WHERE dedup_key = %s AND state = 'failed'
```

The `state = 'failed'` fence guarantees the reset touches only a terminally-failed row: a
freshly-inserted `queued` row, an in-flight `queued`/`running` row, and a `succeeded`/`canceled` row
are all left untouched — so in-flight dedup and no-resurrection hold without extra branching.
`attempt = 0` is required so `dequeue` can claim the recycled job (otherwise it stays at
`max_attempts` and the wedge persists). The trailing `SELECT … WHERE dedup_key` returns the job as
before.

`_enqueue_step` (the sole install/boot enqueue site) passes `retry_terminal_failed=True`. It already
holds the per-Run advisory lock, which serializes concurrent retries on the same Run; the
`state = 'failed'` fence is the additional guard. No other `enqueue` caller passes the flag, so
`provision`, `build`, `teardown`, `reprovision`, `power`, `force_crash`, `capture_vmcore`, and image
build are unchanged.

The recycle is a queue-internal re-admission in raw SQL, consistent with how `queue.py` already
manages job state (worker-fenced raw `UPDATE`s). Jobs are never driven through the
`can_transition`-guarded repository layer (that guards `runs`/`systems`), so `JobState.FAILED` stays
terminal in `domain/capacity/state.py` and the state-adjacency test is unchanged.

## Consequences

- A transient install/boot failure is retried in place by calling `runs.install` / `runs.boot`
  again: the failed job is reset to a fresh `queued` attempt and the worker re-runs the step, with
  no new build and no manual DB edit. `runs.get`'s existing `pending` + `runs.install` suggestion
  becomes truthful.
- In-flight (`queued`/`running`) step jobs are still deduped; a running worker's lease is never
  disturbed by a concurrent re-call (the fence excludes `running`).
- The failed attempt's `error_category` / `failure_context` are cleared on recycle, so the prior
  reason is not preserved on the job row after a retry. Acceptable: the operator just received the
  failure envelope before choosing to retry. The provision path is unaffected (flag off), so
  ADR-0149's "surface the original failed-provision reason via `get_by_dedup_key`" still holds.
- A deterministic install failure (e.g. a genuine `configuration_error`) re-fails with the same
  error on each explicit `runs.install`, bounded by one `max_attempts` cycle per call — no infinite
  loop, because each retry is an operator action, not an automatic re-enqueue.
- No DDL, no migration, no schema change.

## Considered & rejected

- **Per-attempt dedup key (`<run>:install:<n>`).** Rejected: it accumulates dead `failed` rows per
  retry, and it changes the natural key that `get_by_dedup_key` and step bookkeeping read; the
  step's terminal state stops being a single addressable row.
- **Global recycle in `queue.enqueue` (no opt-in).** Rejected: it would reset a failed `provision`
  job on a re-admission, defeating ADR-0149 (admission reads the failed provision job's redacted
  reason via `get_by_dedup_key`, gated on `state == FAILED`). Scoping the flag to the step path
  keeps the blast radius to install/boot.
- **A new `ops.job_requeue` tool / explicit retry verb.** Rejected as unnecessary surface:
  `runs.install` / `runs.boot` are the existing operator affordance and `runs.get` already points at
  them; recycling on re-call delivers the retry without a new tool.
- **Gate recycle on a retryable `error_category`.** Rejected: extra classification the issue did not
  ask for; an explicit re-call of a deterministic failure simply re-fails, which is bounded and
  self-evident.
