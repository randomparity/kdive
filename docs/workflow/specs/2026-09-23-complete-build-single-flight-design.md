# Single-flight external-build finalization

Issue: #2680. Decision: [ADR-0675](../../adr/0675-single-flight-external-build-finalization.md).
Base: `main`. Follow-up owner for durable finalization: #2681.

## Problem

A `runs.complete_build` retry after a client timeout restarts the scan from zero, because the
finalize runs inside the request task on the request's connection. With a 60 s client and a
66 s ppc64le scan, the retries never converge (#2680 has the measured rows).

## Requirements

1. A call for a Run whose finalize is running in the same server process joins that finalize and
   returns its `ToolResponse`. It starts no second scan.
2. A caller cancel does not stop the finalize. The finalize runs to its commit or its rejection.
3. A finalize whose only caller was cancelled still commits and leaves no in-flight entry.
4. A finalize rejection or exception reaches every joined caller. The next call after that
   starts a new finalize.
5. After it acquires the validation slot, the service reads the recorded build result again and
   returns it without calling the validator.
6. Unchanged: the response shape; the upload-window fencing; `upload_window_replaced`; publication
   under the Run lock; the measurement record, one per service call.
7. The `runs.complete_build` docstring tells the agent to call again after a request timeout and
   not to re-mint the window while a finalize runs.

## Design

Owner: `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`, because it holds the pool.

- `_IN_FLIGHT: dict[UUID, asyncio.Task[ToolResponse]]` is a module-level map, process-wide like
  `_EXTERNAL_BUILD_VALIDATION_SLOTS`. The event loop is single-threaded, so the lookup and the
  insert run with no `await` between them and need no lock.
- `complete_build` checks the arguments as now. It then opens one pool connection for
  `_authorize`: `RUNS.get`, the project check, `require_role(CONTRIBUTOR)`, and the recorded-result
  fast path. It releases that connection before it awaits the finalize. A joiner holds no
  connection.
- `_join_or_start` looks up the Run. When no task exists, it creates
  `asyncio.create_task(self._finalize(...))`, stores it, and adds `_forget` as its done-callback.
  When a task exists, it logs `runs.complete_build joined the in-flight finalize for run <id>`. In
  both cases it returns `await asyncio.shield(task)`.
- `_finalize` opens its own pool connection, loads the Run again, returns the recorded result if
  one exists, and otherwise runs the current service call and error mapping. It is the current
  `_complete_authorized_build` body from `CompleteBuildFinalizer(...)` onward, unchanged.
- `_forget(run_id, task)` removes the entry only if it still maps to `task`. When the task ended
  with an exception, it logs that exception once at error level, so an unawaited failure is not
  lost.
- `CompleteBuildFinalizer._validate_uploads` calls `existing_build_result` right after
  `_EXTERNAL_BUILD_VALIDATION_SLOTS.acquire()` and raises `_CompleteBuildAlreadyRecorded` when a
  result exists. `complete()` already maps that exception to `outcome: "already_recorded"`.
- `asyncio.create_task` copies the context, so `bind_context(principal=...)` of the starting caller
  applies inside the task, and the audit row names that caller.

## Failure model

1. **Actors and deployments** — an agent through an MCP client with a 60–300 s request timeout;
   the local-libvirt stack and the Helm chart at the default `server.replicas: 1`.
2. **Invariants and assets at stake** — one publication per upload window; no Run marked
   `succeeded` without a validated upload; the upload window of a re-mint is never deleted; pool
   connections are returned.
3. **Accepted failure classes**
   - A server restart during a finalize loses it; the next call scans again. Same cost as today.
   - With more than one replica, a retry on another replica scans in parallel; the Run lock keeps
     publication single-owner. Bounded CPU cost.
   - A joiner gets the running finalize's result for different `build_id` or `cmdline` arguments.
     Same rule as the existing post-commit idempotency.
   - Server shutdown cancels a running finalize task; its transaction rolls back.
4. **Covered elsewhere** — durable, cross-replica, restart-safe finalize: #2681. Cooperative
   cancellation of the scan thread: ADR-0656, *Cancellation*.

## Testing

Postgres-backed tests use the existing `tests/mcp/complete_build_support.py` helpers and a
validator that blocks on a `threading.Event`:

- cancel the first caller during the scan, then call again: one validator call, one `run_steps`
  build row, the retry is `succeeded`;
- two concurrent callers: exactly one validator call (the adversarial test tightens `in (1, 2)`
  to `== 1`);
- cancel the only caller, release the scan: the Run becomes `succeeded` and `_IN_FLIGHT` is empty;
- a validator that raises once: both joiners get the same failure category, and a third call
  validates again;
- service: a build step recorded before `complete()` reaches the slot returns that result and
  never calls the validator.

A live proof on the ppc64le stack repeats the #2680 scenario with the agent's 60 s client.
