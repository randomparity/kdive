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
  `kdive.mcp.transport_trace` logger. Status and `duration_ms` are captured when
  `http.response.start` is observed; the single line is emitted once, in a `finally`, so a
  request that raises before response-start still logs (with `status=None`) and no request
  ever double-logs.
- **Level independence:** the module gives its logger an explicit `setLevel(logging.INFO)`
  so trace lines emit whenever the flag is on, independent of the root floor
  `KDIVE_LOG_LEVEL` sets (`observability/facade.py`). Without this, `KDIVE_LOG_LEVEL=warning`
  would silently drop every trace line.
- **Gate:** a new registry `Setting` `KDIVE_MCP_TRACE` (`config/core_settings.py`),
  boolean, default off, `group="logging"`, `processes={server}`. When on,
  `server_http_middleware()` prepends `TransportTraceMiddleware` to the list — outermost.
  When off, the middleware is absent entirely.
- **Logged fields** (structured `extra`): `method`, `path`, `mcp_session_id`,
  `mcp_session_id_present`, `mcp_protocol_version`, `status`, `duration_ms`.
- **Redaction:** `Authorization` → `authorization_present` bool only, never the value.
  `Mcp-Session-Id` is a session handle, logged as value. The record still passes the
  existing logging redaction floor (ADR-0090) as defense-in-depth.
- **Timing / `duration_ms` semantics:** `duration_ms` is the interval from request receipt
  to the moment the response status is determined — to `http.response.start` when observed
  (so a long-lived SSE stream does not inflate it or delay the line), else to the exception
  on the failure path. It is therefore always a non-negative latency (never `None`): on a
  normal request it is time-to-response-headers, on a failed request it is time-to-failure.
- **Failure path:** if the downstream app raises before `http.response.start`, the line is
  still emitted (`finally`) with `status=None` and a real `duration_ms` (time to the
  exception), and the exception re-raises.

### Threading the enable flag

`processes/server.py::server_http_middleware()` currently takes no arguments and returns a
fixed one-element list. It gains a `trace_enabled: bool` parameter and appends
`TransportTraceMiddleware` first (outermost) when true. The flag is resolved **once** in
`__main__._handle_server` via `config.require(MCP_TRACE)` — where `config.load()` has
provably already run — and threaded through `run_server(..., trace_enabled=...)` into
`serve_mcp`, exactly like `HTTP_HOST`/`HTTP_PORT` are read in `__main__` and passed as
arguments. This keeps `server_http_middleware()` a pure function of its argument (no hidden
global-config read, no load-order dependency, no global-state mutation in the seam test).

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
2. With `KDIVE_MCP_TRACE` unset/off, `server_http_middleware()` returns the list *without*
   `TransportTraceMiddleware`, and no trace record is emitted.
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
- level independence: with the root logger set to WARNING (mimicking
  `KDIVE_LOG_LEVEL=warning`), a request still emits one trace record — asserts the dedicated
  logger's own INFO level bypasses the raised root floor.
- exactly-one-line: a normal success path emits a single record (no double-log from a
  response-start path plus the `finally`).
- non-`http` scope (`lifespan`/`websocket`) → passthrough, no record.

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

**Optional live assertion (not a PR gate).** A `live_stack` check that drives a request with
a bogus `Mcp-Session-Id` against the running server and asserts the journal shows a
`transport_trace` line with `status=404` — the only vehicle that proves FastMCP's vendored
transport surfaces its session-miss 404 as an observable `http.response.start`. Documented as
a follow-up verification, gated like the other `live_stack` tests; the PR gate stands on the
Starlette-stack composition test above.

## Non-goals

- Request/response body capture or a general protocol debugger (scope guard).
- uvicorn `access_log` toggling (rejected in ADR-0417 as redundant/speculative).
- Any change to the FastMCP tool-call middleware stack.
