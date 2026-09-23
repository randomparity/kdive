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
   starts a new finalize; a finished task still in the map counts as absent.
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
- `_join_or_start` looks up the Run. When no task exists, or the task is already done, it creates
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
   several agents finalizing different Runs at once; the local-libvirt stack and the Helm chart at
   the default `server.replicas: 1`.
2. **Invariants and assets at stake** — one publication per upload window; no Run marked
   `succeeded` without a validated upload; the upload window of a re-mint is never deleted; pool
   connections are returned. Each running finalize holds one pool connection (and, when chunked,
   the Investigation and Run locks) from start to commit, independent of callers; N concurrent
   finalizes hold N of the pool's 10 connections for about the sum of their scans.
3. **Accepted failure classes**
   - A server restart during a finalize loses it; the next call scans again. Same cost as today.
   - With more than one replica, a retry on another replica scans in parallel; the Run lock keeps
     publication single-owner. A chunked retry blocked on the Run lock can also get
     `no_upload_manifest` for a Run the other replica just finalized (the refresh-`None` branch
     has no recorded-result check). Existing behavior, carried to #2681.
   - A caller that re-minted and re-uploaded joins the old window's finalize and gets
     `upload_window_replaced`; the next call validates the current window. One extra round trip.
   - A joiner gets the running finalize's result for different `build_id` or `cmdline` arguments.
     Same rule as the existing post-commit idempotency.
   - Server shutdown cancels a running finalize task; its transaction rolls back.
4. **Covered elsewhere** — durable, cross-replica, restart-safe finalize: #2681. Cooperative
   cancellation of the scan thread: ADR-0656, *Cancellation*.

## Testing

Postgres-backed tests use the existing `tests/mcp/complete_build_support.py` helpers and a
validator that blocks on a `threading.Event`:

- cancel the first caller during the scan, then call again and wait until the `joined the
  in-flight finalize` log line appears before the scan is released: one validator call, one
  `run_steps` build row, the retry is `succeeded`;
- two concurrent callers: exactly one validator call (the adversarial test tightens `in (1, 2)`
  to `== 1`);
- cancel the only caller, release the scan: the Run becomes `succeeded` and `_IN_FLIGHT` is empty;
- a validator that raises once, with the second caller joined (same log barrier): both get the
  same failure category, and a third call validates again;
- re-mint during a blocked finalize, then join: both callers get `upload_window_replaced`, and the
  next call validates the new window;
- service: with the validation slot held by the test, `complete()` queues on it; the test records
  a build step, releases the slot, and gets that result with no validator call.

A live proof on the ppc64le stack repeats the #2680 scenario with the agent's 60 s client.
