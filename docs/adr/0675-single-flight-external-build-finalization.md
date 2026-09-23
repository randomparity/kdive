# 0675 — Single-flight external-build finalization

## Status

Proposed

## Context

ADR-0656 keeps `runs.complete_build` synchronous and gives one recovery rule for a dropped
request: call `complete_build` again, which redoes the work if no result was recorded. It names
a client with a shorter timeout as a reopening condition.

The 0.5.0 ppc64le validation (issue #2680) met that condition. The agent's MCP client cancels a
request after 60 s. After the three-pass fusion (#2569, #2570, #2571), a 2.1 GB ppc64le bundle
scans in 66 s. Eight consecutive calls were cancelled at about 60.0 s, each 1.9 GB into the
scan. A cancel discards the request's work, so under the ADR-0656 rule the retries do not
converge. One call finished only after its session closed. A retry that arrived meanwhile
scanned the committed Run again for 54 s.

The cause is ownership. The finalize runs inside the request task on the request's pool
connection, so the request's lifetime bounds it. The scan thread outlives a cancel, but its
result has no owner.

## Decision

The `runs.complete_build` handler runs each finalize as a process-local task that owns its own
pool connection. The handler keeps at most one task per Run. A caller authorizes on a short
connection of its own, then awaits the Run's task through `asyncio.shield`. If no task exists
for the Run, the caller starts one; otherwise the caller joins the existing task. A caller cancel
stops only that caller's wait. The task runs to its commit or its rejection, and every caller
awaiting it gets the same `ToolResponse`. The task removes its own entry when it ends.

The finalize service also reads the recorded build result again after it acquires the
validation slot, and returns that result without a scan.

The recovery rule becomes: after a request timeout, call `complete_build` again for the same
Run. Do not re-mint the upload window. The call joins the running finalize or returns the
recorded result.

## Consequences

- With a 60 s client and a 66 s scan, the first retry returns `build_ref`. In general, a finalize
  needs `ceil(scan / client_timeout) - 1` retries, and each retry makes progress.
- A joiner receives the running finalize's result even when its own `build_id`, `cmdline`, or
  source label differs. This extends the existing rule that a call after the commit returns the
  recorded result and ignores its own arguments.
- The measurement record stays one per finalize. A joiner reaches no service code and emits only
  an info log line, so a scan is never counted twice.
- A joiner that re-minted and re-uploaded joins the old window's finalize, which ends with
  `upload_window_replaced`. The next call starts a new finalize against the current window. The
  join key is the Run, not the (Run, window) pair of ADR-0656 *Retry and idempotency*.
- A finalize whose callers are all gone still publishes. The Run then shows `succeeded` to
  `runs.get` and to the next `complete_build`.
- Each running finalize holds one pool connection from start to commit, independent of its
  callers, and on the chunked path also the Investigation and Run locks. N Runs finalizing at
  once hold N of the server pool's 10 connections (`src/kdive/db/pool.py`), for about the sum of
  their scans, because the scans share one validation slot.
- The post-slot recheck emits an `already_recorded` measurement record with no scan, so a count of
  records is no longer a count of scans; `scan_ms` tells them apart.
- The state lives in one server process. A restart loses a running finalize. A retry that reaches
  another replica starts a second scan; the Run lock at publish keeps the result correct. Issue
  #2681 owns a durable finalize for those cases, with its trigger conditions.
- The scan thread still has no cooperative cancellation (ADR-0656, *Cancellation*). A cancel no
  longer orphans a scan, so that gap no longer wastes a scan.
- The response shape, the upload-window fencing, `upload_window_replaced`, and single-owner
  publication under the Run lock do not change.

## Considered & rejected

- **Do nothing and document a longer client timeout.** judgment: fit — the server cannot see or
  raise a client's timeout (ADR-0656), so a 60 s client stays in a retry loop.
- **Durable job-based finalization now.** judgment: cost — a new job kind, migration, worker
  handler, and public contract change for a failure the in-process owner removes at the default
  single replica; #2681 records when it becomes necessary.
- **MCP progress notifications to extend the client timeout.** verified: the MCP TypeScript SDK
  documents `resetTimeoutOnProgress` as an optional per-call client option and 60 s as the
  protocol default (`docs/clients/calling.md`,
  github.com/modelcontextprotocol/typescript-sdk, main, read 2026-09-23); the server cannot set
  that option for the client.
- **Shield a detached finalize with no map, and rely on the post-slot recheck.** judgment: fit —
  a concurrent caller then queues on the slot and scans the Run again after a failed finalize
  instead of receiving the shared failure, which #2680 requires.
- **Cut the scan time below 60 s.** verified: the rows in #2680 show 4.5 s of store wait in a
  66 s scan after the pass fusion, so the rest is gunzip, tar, and hashing on one core; a bundle
  near the 2 GiB limit (`_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES`,
  `src/kdive/build_artifacts/validation.py:69`) stays above 60 s on that host.
