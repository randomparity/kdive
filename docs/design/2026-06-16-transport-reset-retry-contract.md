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
`MAX_WAIT_S = 300.0` s (`src/kdive/mcp/tools/jobs.py`). FastMCP's uvicorn server applies
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

Three changes, all additive and confined to the transport-config + jobs long-poll + guide docs.
The **documented retry contract (change 3) is the primary mitigation** for the reported D3
resets. Changes 1 and 2 are supporting, in-control adjustments; neither alone resolves the raw
in-flight drop (which is physically unwrappable, per the Problem section).

### 1. Explicit uvicorn keepalive (`src/kdive/__main__.py`)

`_run_server` passes `uvicorn_config={"timeout_keep_alive": _HTTP_KEEPALIVE_S}` to
`app.run_async(transport="http", host=host, port=port, …)`. `_HTTP_KEEPALIVE_S = 65.0` — a module
constant sized just above the common 60 s proxy idle default.

`timeout_keep_alive` governs only how long the **server** holds an *idle keepalive TCP
connection* open *between* requests on that same connection. Its benefit is therefore
**conditional**: it helps only a client that **reuses the same TCP connection** across successive
short polls (reducing reconnect churn between polls). It does **nothing** for a client that opens
a fresh connection per `jobs.wait`, and it does **not** extend the in-flight lifetime of a single
long hold (no uvicorn knob does). `_HTTP_KEEPALIVE_S` (65 s) intentionally sits *below*
`MAX_WAIT_S` (300 s): it bounds the idle between-poll gap, not the in-flight hold, so there is no
expectation that it protects a single 300 s wait. This is a best-effort churn reduction, not a fix
for the raw drop — the contract (change 3) is the fix.

`run_async(**transport_kwargs)` forwards to `run_http_async(..., uvicorn_config=...)`, which merges
the dict into `uvicorn.Config` (verified against fastmcp-slim 3.4.0). No other `run_async`
arguments change.

**Test seam.** `_run_server` `await`s `app.run_async(...)`, which blocks until the server stops, so
a test cannot call `_run_server` to assert the kwarg. The `uvicorn_config` dict is built by a small
pure helper, `_server_uvicorn_config() -> dict[str, object]`, that the unit test calls directly and
asserts equals `{"timeout_keep_alive": 65.0}` — testing the real value, not a stub. `_run_server`
calls the helper, keeping its body trivial.

### 2. `jobs.wait` prompt non-terminal return (`src/kdive/mcp/tools/jobs.py`)

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

- `_server_uvicorn_config()` returns `{"timeout_keep_alive": 65.0}` and `_run_server` passes it as
  `uvicorn_config=` to `app.run_async`; a unit test asserts on the helper's return value directly
  (no forever-blocking `run_async` mock).
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
