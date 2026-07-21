# Spec: Opt-in ASGI transport-trace logging (#1391)

- Issue: #1391
- ADR: [ADR-0417](../adr/0417-opt-in-asgi-transport-trace.md)
- Status: Draft → for implementation

## Problem

kdive has no server-side visibility into the HTTP/transport layer where the MCP session
lifecycle, the `initialize` handshake, `resources/*` reads, and transport-level status
codes (e.g. `404 Session not found`) happen. Neither the FastMCP tool-call middleware
(above the transport) nor the single existing ASGI entry (`BareBearerHintMiddleware`)
logs a general request/response trace. An operator cannot answer, from kdive's own logs,
whether a client sent an `Mcp-Session-Id`, what status the server returned, or whether the
client re-`initialize`d after a 404. Proving an MCP client's conformance to the spec's
"on 404 to a request carrying `Mcp-Session-Id`, the client MUST start a new session"
requirement needs exactly this server-side transport visibility.

## Goal

An opt-in, redaction-safe ASGI middleware that logs one structured line per HTTP request,
sufficient to reconstruct a session lifecycle from server logs alone, at zero cost when
disabled.

## Design

See ADR-0417 for the decision and rejected alternatives. Summary:

- **`TransportTraceMiddleware`** (`src/kdive/mcp/middleware/transport_trace.py`): a pure
  ASGI callable that wraps `send` to capture the response status, times the request with
  `time.monotonic()`, and logs exactly one INFO record per HTTP request via the dedicated
  `kdive.mcp.transport_trace` logger. The line is emitted **when `http.response.start` is
  observed** (at the response headers), setting an `emitted` flag; a `finally` emits the
  line **only** when response-start was never seen (the pre-header failure path). Emitting at
  response-start — not in the `finally` — is required for real-time observation: for a
  long-lived SSE response `await self.app(...)` returns only at stream close, so a
  finally-only emission would withhold the line for the whole stream. The `emitted` flag
  makes the two paths mutually exclusive, so a request logs exactly once.
- **Per-request state is call-local.** Starlette instantiates the middleware once and runs
  `__call__` concurrently for every in-flight request, so `emitted`, the captured status, and
  the receipt timestamp are locals of each `__call__` (captured by the nested `send` wrapper
  via `nonlocal`), **never** instance attributes on `self` — otherwise concurrent requests
  race and log each other's status/duration. This mirrors `BareBearerHintMiddleware`, which
  holds only `self.app` plus per-call locals.
- **Level independence:** the module gives its logger an explicit `setLevel(logging.INFO)`
  so trace lines emit whenever the flag is on, independent of the root floor
  `KDIVE_LOG_LEVEL` sets (`observability/facade.py`). Without this, `KDIVE_LOG_LEVEL=warning`
  would silently drop every trace line.
- **Gate:** a new registry `Setting` `KDIVE_MCP_TRACE` (`config/core_settings.py`),
  `group="logging"`, `processes={server}`, declared the way the existing default-off gates
  `OTEL_ENABLED`/`FAULT_INJECT` are — `parse=_str`, **no default** (resolves to `None` when
  unset) — and read via `config.get` plus the codebase's **set-membership** truthy idiom,
  `(config.get(MCP_TRACE) or "").strip().lower() in {"1","true","yes","on"}` (mirroring
  `otlp_enabled()` in `facade.py`). A bare `bool(config.get(...))` is wrong: `parse=_str`
  returns the raw string and `bool("0")`/`bool("false")` are `True`, so it would *enable*
  tracing for the exact values an operator sets to disable it. Read via `config.get`, never
  `config.require` (which raises `CONFIGURATION_ERROR` on an unset no-default setting and
  would break every normal boot where the flag is off). When on, `server_http_middleware()`
  prepends `TransportTraceMiddleware` to the list — outermost. When off, the middleware is
  absent entirely.
- **Logged fields** (structured `extra`): `method`, `path`, `mcp_session_id`,
  `mcp_session_id_present`, `mcp_protocol_version`, `status`, `duration_ms`.
- **Redaction:** `Authorization` → `authorization_present` bool only, never the value.
  `Mcp-Session-Id` is a session handle, logged as value. The record still passes the
  existing logging redaction floor (ADR-0090) as defense-in-depth.
- **Timing / `duration_ms` semantics:** `duration_ms` is `monotonic()` minus request-receipt
  measured **at the point the line is emitted** — at `http.response.start` for the normal
  line (time-to-response-headers, so a long-lived SSE stream does not inflate it or delay the
  line), or in the `finally` for the pre-header failure line. Always a non-negative number
  (never `None`).
- **Pre-header failure path (covers cancellation):** if the downstream app raises **or is
  cancelled before `http.response.start`** (a request aborted during dispatch), `emitted` is
  still false, so the `finally` emits the line with `status=None`, then the
  exception/cancellation re-raises. `duration_ms` is computed in that `finally`, **not**
  inside an `except` handler — which would miss `asyncio.CancelledError` (a `BaseException`),
  leaving the field unset.
- **Post-header stream disconnect is not a separate line (deliberate scope).** An SSE
  response sends `http.response.start` (e.g. `200`) *before* streaming events, so a client
  that disconnects mid-stream raises `CancelledError` when `emitted` is already true: the
  `finally` emits nothing and only the opening line (status + TTFB) survives. Stream-close
  accounting is out of scope for the request/response-envelope lifecycle this targets; see
  ADR-0417's rejected two-line open+close alternative.

### Threading the enable flag

`processes/server.py::server_http_middleware()` currently takes no arguments and returns a
fixed one-element list. It gains a `trace_enabled: bool` parameter and prepends
`TransportTraceMiddleware` (outermost) when true. The flag is resolved **once** in
`__main__._handle_server` via the `config.get(MCP_TRACE)` set-membership check above — where
`config.load()` has provably already run — and threaded through
`run_server(..., trace_enabled=...)` into `serve_mcp`, exactly like `HTTP_HOST`/`HTTP_PORT`
are read in `__main__` and passed as arguments (`trace_enabled` is keyword-only so
`run_server` stays within the positional-parameter limit). This keeps
`server_http_middleware()` a pure function of its argument (no hidden global-config read, no
load-order dependency, no global-state mutation in the seam test).

### Level-independence invariant

The design relies on the OTel `LoggingHandler` bridge that `observability/facade.py`
installs on the root logger staying at its default `NOTSET` level: a record the dedicated
`kdive.mcp.transport_trace` logger (own level `INFO`) creates then propagates to that root
handler, and a `NOTSET` handler processes it regardless of the root *logger* level
`KDIVE_LOG_LEVEL` set. This invariant is documented here so a future change that gives the
bridge handler a level does not silently drop trace lines.

## Acceptance criteria

1. With `KDIVE_MCP_TRACE=1`, an HTTP request to the MCP server produces exactly one
   `kdive.mcp.transport_trace` INFO log record carrying `method`, `path`,
   `mcp_session_id_present` (and `mcp_session_id` value when present),
   `mcp_protocol_version`, `status`, and a non-negative numeric `duration_ms`.
2. With `KDIVE_MCP_TRACE` unset **or** set to a falsey string (`0`/`false`/`off`),
   `server_http_middleware()` returns the list *without* `TransportTraceMiddleware`, and no
   trace record is emitted.
3. The `Authorization` header value never appears in any trace record; only a presence
   boolean is logged.
4. A request short-circuited before dispatch (e.g. a bare-bearer 401, or a 404 session
   miss) is still traced with its status — trace is outermost. Proven through a real
   Starlette middleware stack (not just a list-position assertion), so "first in the list =
   outermost" is verified at runtime.
5. A downstream error that sends no `http.response.start` still produces a trace line with
   `status=None` and a non-negative numeric `duration_ms`.
6. `KDIVE_MCP_TRACE` appears in the generated config reference.
7. `just ci` is green (lint, type, tests, doc guards).

## Test plan

Unit tests over the ASGI callable and the seam, exercised without a live transport by
driving `TransportTraceMiddleware(app)(scope, receive, send)` with hand-built ASGI
`scope`/`receive`/`send` and a `caplog` capture:

- happy path: GET with `Mcp-Session-Id` + `MCP-Protocol-Version` → one record, fields
  populated, `mcp_session_id_present=True`, status from a stub 200 response.
- no session header → `mcp_session_id_present=False`, no `mcp_session_id` value.
- `Authorization: Bearer <token>` present → `authorization_present=True`, token string
  absent from the record (assert the token substring is not in the formatted output).
- downstream raises before `http.response.start` → exactly one record with `status=None`
  and a numeric `duration_ms >= 0`, and the exception propagates (not swallowed).
- downstream cancelled (`asyncio.CancelledError`) **before** `http.response.start` → exactly
  one record with `status=None` and a numeric `duration_ms >= 0`, and the `CancelledError`
  propagates — proves duration is computed in the `finally`, not an `except Exception` that
  a `BaseException` would skip.
- post-header disconnect: a stub that sends `http.response.start` (200) then raises
  `CancelledError` mid-stream → the single record already emitted carries `status=200` and
  the `finally` adds no second line (documents the deliberate stream-close scope decision).
- level independence: with the root logger set to WARNING (mimicking
  `KDIVE_LOG_LEVEL=warning`), a request still emits one trace record — asserts the dedicated
  logger's own INFO level bypasses the raised root floor.
- bridge-`NOTSET` regression guard: assert the OTel `LoggingHandler` that
  `observability/facade.py` installs on the root logger is at level `NOTSET`, so the
  documented level-independence invariant cannot silently regress (a handler-level would
  gate emission in production while the caplog test stayed green).
- exactly-one-line: a normal success path emits a single record (no double-log from a
  response-start path plus the `finally`).
- concurrency: two requests interleaved through **one** `TransportTraceMiddleware` instance,
  driven with `asyncio.gather` where request A's inner app awaits a test-controlled event
  released only *after* request B has entered `__call__` and captured its own state — so the
  test provably fails against a `self`-attribute implementation (a non-interleaving test
  passes for both correct and buggy code, a false green). Assert each record's
  status/session-id/duration matches the request that produced it, and exactly one line per
  request.
- non-`http` scope (`lifespan`/`websocket`) → passthrough, no record.

**Gate resolution (closes the string→bool path AC2 alone misses).** Drive the
`__main__`-side resolver (the `config.get(MCP_TRACE)` set-membership check):

- `KDIVE_MCP_TRACE` unset → resolves False → `TransportTraceMiddleware` absent.
- `KDIVE_MCP_TRACE=0` and `=false` → resolve False → middleware absent (guards the
  `bool("0") is True` trap).
- `KDIVE_MCP_TRACE=1`/`true`/`yes` → resolve True → middleware present as the first entry.

**Composition / ordering (closes AC4 at runtime).** A real `starlette.applications.Starlette`
built with `middleware=server_http_middleware(trace_enabled=True)` around an inner ASGI app,
driven by `starlette.testclient.TestClient` (in-process, no live server, no DB):

- an inner app that returns a plain 404 → the trace record carries `status=404`, proving the
  trace runs outermost over a short-circuited response and that Starlette applies list
  position 0 as the outermost wrapper.
- a request that trips `BareBearerHintMiddleware`'s 401 (bare-JWT `Authorization`) with the
  trace middleware also present → the trace record carries `status=401`, proving the trace
  observes a peer middleware's short-circuit.

**Seam:** `server_http_middleware(trace_enabled=True)` includes `TransportTraceMiddleware`
as the first entry and `trace_enabled=False` excludes it — a pure function of the argument,
no config-global read.

**FastMCP-stack 404 (PR gate — proves the one job).** The raw-Starlette composition test
above proves list-position ordering but *not* that the middleware list `app.run_async` /
`app.http_app` receives sits outside FastMCP's vendored transport mount — the load-bearing
assumption for observing a session-miss `404 Session not found`. A PR-gated test assembles
the **real** app (`build_app(...).http_app(middleware=server_http_middleware(trace_enabled=True))`,
matching `processes/server.py`'s wiring) and drives an in-process request bearing a bogus
`Mcp-Session-Id` via `starlette.testclient.TestClient`, asserting a `transport_trace` record
with `status=404`. This runs in the integration tier (a disposable pool; the session-miss is
rejected by the transport before any DB access — the app pool can be constructed unopened),
which CI hard-gates via `KDIVE_REQUIRE_DOCKER=1`. If FastMCP's session-miss cannot be
reproduced in-process, the fallback PR gate asserts, against the assembled ASGI app, that
`TransportTraceMiddleware` wraps FastMCP's transport mount (a position check on the *real*
stack, not a raw-Starlette one) — so the "outermost over FastMCP's transport" assumption is
always checked by something the PR must pass, never only by a de-gated live test.

**Live assertion (`live_stack`, additional proof).** A `live_stack` check drives a
bogus-`Mcp-Session-Id` request against the running server and asserts the journal shows a
`transport_trace` line with `status=404` — end-to-end confirmation over the real uvicorn
transport, gated like the other `live_stack` tests. Belt-and-suspenders on top of the
in-process FastMCP-stack gate, not the sole proof.

## Non-goals

- Request/response body capture or a general protocol debugger (scope guard).
- uvicorn `access_log` toggling (rejected in ADR-0417 as redundant/speculative).
- Any change to the FastMCP tool-call middleware stack.
