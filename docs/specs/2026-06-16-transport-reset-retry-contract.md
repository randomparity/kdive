# Spec — Transport-reset retry contract for long-polls (#483)

- **Issue:** [#483](https://github.com/randomparity/kdive/issues/483)
- **ADR:** [`0138`](../adr/0138-transport-reset-retry-contract.md)
- **Date:** 2026-06-16

## Problem

Black-box MCP testing (D3) saw four calls (#18, #27, #29, #38) fail with a raw
`socket connection was closed unexpectedly` message — a `fetch()`-level transport drop, not a
kdive `ToolResponse` envelope with `error_category`/`retryable`. All four succeeded on a bare
retry, so they were transient resets of a held stream, but the client had no signal that they
were transient: a raw socket close carries no `retryable` flag.

The held stream is `jobs.wait`. It keeps one streamable-HTTP POST open while it polls, up to
`MAX_WAIT_S = 300.0` s (`src/kdive/mcp/tools/catalog/jobs.py`). FastMCP's uvicorn server applies
no per-request duration cap to an in-flight streaming POST, so the only thing that severs a long
hold is an intermediary (reverse proxy / load balancer) idle timeout. Once severed, the response
bytes never arrive — kdive cannot wrap that drop in an envelope, because the connection that would
carry the envelope is gone.

### What is and is not in kdive's control

- **In control:** the uvicorn keepalive between successive polls; whether `jobs.wait` returns
  promptly at its cap as a "still running, call again" signal instead of holding idle to the
  deadline; the documented retry contract.
- **Not in control:** wrapping a mid-stream TCP reset of an in-flight long hold. The bytes are
  gone. No uvicorn knob extends in-flight stream lifetime past a proxy that cuts it.

The in-process synchronous-stall variant of "raw socket drop" (a fast tool blocking the event
loop) is a **separate** failure mode owned by ADR-0126 (#481). This issue is the long-poll/proxy
axis only and does not touch the dispatch boundary.

## Decision (per ADR-0138)

Three changes, all additive and confined to the transport-config + jobs long-poll + guide docs:

### 1. Explicit uvicorn keepalive (`src/kdive/__main__.py`)

`_run_server` passes `uvicorn_config={"timeout_keep_alive": _HTTP_KEEPALIVE_S}` to
`app.run_async(transport="http", host=host, port=port, …)`. `_HTTP_KEEPALIVE_S = 65.0` — a module
constant sized just above the common 60 s proxy idle default so the keepalive connection survives
the gap between the rapid short-wait polls the contract recommends. It does **not** extend an
in-flight hold; the spec and ADR say so explicitly.

`run_async(**transport_kwargs)` forwards to `run_http_async(..., uvicorn_config=...)`, which merges
the dict into `uvicorn.Config` (verified against fastmcp-slim 3.4.0). No other `run_async`
arguments change.

### 2. `jobs.wait` prompt non-terminal return (`src/kdive/mcp/tools/catalog/jobs.py`)

No behavior change — `wait_job` already returns the current `running`/`queued` envelope (with
`jobs.wait` in `suggested_next_actions`) the moment the clamped deadline passes without the job
going terminal. We:

- Make the "still running, call again" contract explicit in the `wait_job` docstring.
- Add a test that pins it: a non-terminal job with a tiny `timeout_s` returns promptly with the
  non-terminal envelope and `jobs.wait` in `suggested_next_actions`, so a future change cannot
  silently hold the stream to the deadline.

The default `timeout_s` (30 s) and `MAX_WAIT_S` (300 s) are unchanged: the default is already well
under any normal proxy timeout, and the 300 s cap stays an explicit opt-in.

### 3. Retry-contract documentation (`docs/guide/async-jobs.md`, `docs/guide/errors.md`)

Add a "Transport resets and retries" subsection to `async-jobs.md` stating:

- Long-poll/transport resets are transient; a raw `socket connection was closed unexpectedly`
  from a held `jobs.wait` (or any idempotent read) is safe to retry unchanged.
- The token-efficient pattern is repeated short `jobs.wait` calls (default 30 s), re-issued while
  the returned envelope is non-terminal, rather than one 300 s hold. A non-terminal `jobs.wait`
  returns the current envelope with `jobs.wait` in `suggested_next_actions` — that *is* the
  "call again" signal.
- A long explicit `timeout_s` (up to 300 s) risks an intermediary reset; that drop is retryable.

Add a transient-reset recovery note to `errors.md` so the `transport_failure` row points at the
same contract (a server-observable recoverable stream error already returns
`transport_failure`/`retryable=true`).

## Acceptance criteria (reviewer-checkable)

- `_run_server` passes `uvicorn_config={"timeout_keep_alive": _HTTP_KEEPALIVE_S}` with
  `_HTTP_KEEPALIVE_S == 65.0`; a unit test asserts the kwarg is forwarded to `run_async`.
- A `wait_job` test asserts a non-terminal job with a small `timeout_s` returns promptly (well
  before `MAX_WAIT_S`) with a non-terminal envelope carrying `jobs.wait` in
  `suggested_next_actions`.
- `docs/guide/async-jobs.md` documents the transport-reset retry contract; `docs/guide/errors.md`
  cross-references it from `transport_failure`.
- No schema, migration, new MCP tool, env var, or envelope-shape change. `just ci` green.

## Out of scope

- The dispatch-boundary synchronous-stall timeout (ADR-0126 / #481).
- Lowering `MAX_WAIT_S` or the default `timeout_s` (ADR-0138 rejected alternatives).
- A tunable `KDIVE_HTTP_KEEPALIVE_S` setting (no user need today; module constant).
- Any client-library change — kdive ships the server; the contract is for client authors.
