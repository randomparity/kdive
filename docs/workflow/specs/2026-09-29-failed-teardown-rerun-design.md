# `systems.teardown` re-runs a dead-lettered ordinary teardown job (#2929)

## Scope and authority

Campaign 60f31b4f6024, issue #2929, token `q2929-9d953ace`. Operator decision (2026-09-29): an
operator-initiated re-run through `systems.teardown`, recycling a `failed` `{uid}:teardown` row for
any System that is not `torn_down`; no reconciler lane. Approved exclusions: a reconciler lane for
`failed` Systems (operator); authority teardown recycle and the queued exhausted row (#2917). The
decision is a dated amendment to [ADR-0435](../../adr/0435-reclaim-failed-provision-artifacts.md).
It reverses the accepted class in the #2908 design's Failure model
([`2026-09-29-failed-system-teardown-design.md`](2026-09-29-failed-system-teardown-design.md))
that a failed System's dead-lettered teardown is not re-run.

## Problem

The ordinary path of `teardown_system` (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) replays
any prior `{uid}:teardown` row (`dedup_replay` and `queue.enqueue` under `JobRecyclePolicy.NEVER`).
Once that job dead-letters to `failed`, every later call returns the dead job and no work runs.
For a `failed` System no reconciler lane re-runs it either, so a domain, overlay, or console
artifacts left by a failed provision stay unreclaimed. The same holds for a job that dead-lettered
with `infrastructure_failure` before #2913.

## Design

1. `JobRecyclePolicy` gains `FAILED = "failed"`: `queue.enqueue_with_status` resets a `failed` row
   only. `succeeded`, `canceled`, `queued`, and `running` rows are returned unchanged. The
   `_idempotency._RECYCLED` table maps it to `{JobState.FAILED}`, so `dedup_replay` agrees with the
   enqueue by construction. `TERMINAL` would also reset `succeeded`, so a repeat call on a failed
   System whose teardown already succeeded would enqueue new work on every call.
2. The ordinary path passes `recycle=JobRecyclePolicy.FAILED` to both `dedup_replay` and
   `queue.enqueue`. That path runs only for a System that is not `torn_down`, has no external-boot
   activation, and has no authority binding. The reset stays behind the System advisory lock, the
   ADMIN gate, and `check_external_boot_admission`, in that order.
3. The keyed path is unchanged: a stored envelope under the same `idempotency_key` replays before
   the dedup read. A caller who wants a re-run sends a new key or none.
4. The reset keeps the row id, `authorizing`, and `max_attempts`, and zeroes `attempt`,
   `error_category`, and `failure_context` (existing `enqueue` semantics).

## Success

- A System that is not `torn_down` (`failed`, `ready`, `tearing_down`) whose `{uid}:teardown` row is `failed` with any `error_category`,
  `infrastructure_failure` included, gets that same job id back as `queued` with `attempt = 0`.
- For a `failed` System, a `queued`, `running`, `succeeded`, or `canceled` prior row replays with
  its state, attempt, and payload unchanged.
- `enqueue(..., recycle=FAILED)` resets a `failed` row and leaves a `succeeded` or `canceled` row
  unchanged.

## Failure model

1. **Actors and deployments**
   - a project ADMIN calling `systems.teardown` over MCP;
   - other `{uid}:teardown` writers: the job worker, the orphan and stalled-teardown reconciler
     lanes, breakglass.
2. **Invariants and assets at stake**
   - one `{uid}:teardown` row per System (`jobs.dedup_key` UNIQUE); a live row is never reset;
   - `canceled` stays an operator stop;
   - ADR-0441's orphan-lane exclusion of `failed` Systems and its overlay-absence gate.
3. **Accepted failure classes**
   - the reset job keeps its original `authorizing` principal; the caller is recorded by the tool
     audit, as for the ADR-0620 authority re-run.
   - each call after a new dead-letter re-runs the job; this is bounded by operator calls and the
     handler is idempotent (#2913).
   - a live row that dead-letters after the dedup read is returned in its stale live state; the
     next call resets it. No writer moves a row out of `failed` without the System lock, so the
     read-then-UPDATE window cannot reset anything but a `failed` row.
4. **Covered elsewhere**
   - authority-owned and external-boot teardown: #2917 and ADR-0620;
   - an exhausted `running` row whose lease lapsed: `repair_abandoned_jobs` dead-letters it to
     `failed`, after which this path resets it;
   - an orphaned `ready` System whose teardown dead-lettered: this tool, or the operator;
   - `ops.force_teardown` (breakglass, `enqueue_control_teardown`) keeps `NEVER` and replays a
     dead job; a project ADMIN re-runs it through this tool (follow-up candidate);
   - a `tearing_down` System's dead job: also reset by `repair_stalled_tearing_down_systems`
     under the same System lock and dedup key.

## Considered and rejected

- **Reconciler lane for `failed` Systems.** judgment: operator decision; it would revisit
  ADR-0441's exclusion.
- **`TERMINAL` policy.** judgment: re-enqueues a succeeded teardown on every repeat call.
- **Read the row and pick a policy per state.** judgment: duplicates `dedup_replay` and breaks its
  "pass the same policy as `enqueue`" contract.
