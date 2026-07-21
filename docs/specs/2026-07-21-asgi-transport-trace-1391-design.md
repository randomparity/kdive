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
  boolean, default off, `group="logging"`, `processes={server}`. `server.py`'s
  `server_http_middleware()` reads it (through the config registry) and prepends
  `TransportTraceMiddleware` to the list — outermost — only when the flag is on. When off,
  the middleware is absent entirely.
- **Logged fields** (structured `extra`): `method`, `path`, `mcp_session_id`,
  `mcp_session_id_present`, `mcp_protocol_version`, `status`, `duration_ms`.
- **Redaction:** `Authorization` → `authorization_present` bool only, never the value.
  `Mcp-Session-Id` is a session handle, logged as value. The record still passes the
  existing logging redaction floor (ADR-0090) as defense-in-depth.
- **Timing point:** duration is measured to `http.response.start` (time-to-response
  headers), not to stream completion, so a long-lived SSE response never delays the line.
- **Failure path:** if the downstream app raises before `http.response.start`, the line is
  still emitted (`finally`) with `status=None`.

### Threading the config read

`processes/server.py::server_http_middleware()` currently takes no arguments and returns a
fixed one-element list. It gains the trace entry conditionally. The flag is read via the
config registry (`config.get(MCP_TRACE)`), consistent with how `HTTP_HOST`/`HTTP_PORT` are
read in `__main__.py`. `server_http_middleware()` stays pure by reading the flag itself at
call time (invoked once at server start), matching the existing no-argument signature.

## Acceptance criteria

1. With `KDIVE_MCP_TRACE=1`, an HTTP request to the MCP server produces exactly one
   `kdive.mcp.transport_trace` INFO log record carrying `method`, `path`,
   `mcp_session_id_present` (and `mcp_session_id` value when present),
   `mcp_protocol_version`, `status`, and `duration_ms`.
2. With `KDIVE_MCP_TRACE` unset/off, `server_http_middleware()` returns the list *without*
   `TransportTraceMiddleware`, and no trace record is emitted.
3. The `Authorization` header value never appears in any trace record; only a presence
   boolean is logged.
4. A request short-circuited before dispatch (e.g. a bare-bearer 401, or a 404 session
   miss) is still traced with its status — trace is outermost.
5. A downstream error that sends no `http.response.start` still produces a trace line with
   `status=None`.
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
- downstream raises before `http.response.start` → exactly one record with `status=None`,
  and the exception propagates (not swallowed).
- level independence: with the root logger set to WARNING (mimicking
  `KDIVE_LOG_LEVEL=warning`), a request still emits one trace record — asserts the dedicated
  logger's own INFO level bypasses the raised root floor.
- exactly-one-line: a normal success path emits a single record (no double-log from a
  response-start path plus the `finally`).
- non-`http` scope (`lifespan`/`websocket`) → passthrough, no record.
- `server_http_middleware()` with the flag on includes `TransportTraceMiddleware` as the
  first (outermost) entry; with it off, the list excludes it.

## Non-goals

- Request/response body capture or a general protocol debugger (scope guard).
- uvicorn `access_log` toggling (rejected in ADR-0417 as redundant/speculative).
- Any change to the FastMCP tool-call middleware stack.
