# ADR-0417: Opt-in ASGI transport-trace middleware for MCP session/HTTP debugging

Status: Accepted

## Context

kdive is a multi-user streamable-HTTP MCP service, but it has no server-side visibility
into the HTTP/transport layer — the layer where the MCP session lifecycle, the
`initialize` handshake, `resources/*` reads, and transport-level status codes (e.g.
`404 Session not found`) actually happen (#1391).

Two middleware layers exist and neither observes the transport:

- The **FastMCP tool-call middleware** (`mcp/middleware/`: `telemetry.py`, `usage.py`, …)
  hooks `on_call_tool`. It fires only for a successfully-dispatched tool call — *above*
  the transport. It cannot see `initialize`, `resources/list`/`read`, the HTTP status,
  the `Mcp-Session-Id` header, or a request that fails at the session layer with 404
  (which never reaches `on_call_tool`).
- The **ASGI (Starlette) layer**, wired through `server_http_middleware()`
  (`processes/server.py`), is the correct layer to see raw HTTP but today holds one
  narrow entry (`BareBearerHintMiddleware`, ADR-0380). There is no general request trace.

The session lifecycle and the 404 live inside FastMCP's vendored streamable-HTTP
transport, below both layers. An operator cannot answer, from kdive's own logs, "did the
client send an `Mcp-Session-Id`? what status did we return? did the client re-`initialize`
after a 404?" — the diagnostic that motivated this work (proving an MCP client's
non-recovery on `404 Session not found`).

## Decision

Add an opt-in ASGI request/response trace middleware, `TransportTraceMiddleware`
(`mcp/middleware/transport_trace.py`), wired through the existing
`server_http_middleware()` seam.

- **Pure-ASGI, response-observing.** Like `BareBearerHintMiddleware`, it is a plain ASGI
  callable, not a FastMCP `Middleware`. It wraps `send` to capture the response status
  from the `http.response.start` message, and times the request with a monotonic clock.
- **Outermost in the seam.** It is the *first* entry in `server_http_middleware()`, so it
  is the outermost wrapper and observes every HTTP request — including the ones
  `BareBearerHintMiddleware` short-circuits with a 401, and requests the transport rejects
  with 404.
- **Explicitly gated, off by default.** A new registry `Setting` `KDIVE_MCP_TRACE`
  (boolean, default off, `group="logging"`, `processes={server}`) gates it. When off, the
  middleware is **not added to the list** — not a per-request branch — so a normal
  deployment pays nothing.
- **One structured line per request.** On response start it logs one INFO record via the
  dedicated `kdive.mcp.transport_trace` logger carrying: `method`, `path`,
  `mcp_session_id` (value) + `mcp_session_id_present`, `mcp_protocol_version`, `status`,
  and `duration_ms`. INFO (not DEBUG) so the trace appears once the flag is on regardless
  of `KDIVE_LOG_LEVEL`.
- **Redaction-safe.** The `Authorization` header is logged as `authorization_present`
  (bool) only — never the value, honoring the mandatory-redaction invariant. The
  `Mcp-Session-Id` is a session handle (not an auth credential) and is logged as its
  value. As defense-in-depth the record still passes the existing logging redaction floor
  (ADR-0090), so a token that somehow reached a logged field is scrubbed.
- **Duration = time to response headers.** Measured from request receipt to the
  `http.response.start` emission (time-to-first-byte), so a long-lived SSE stream never
  delays or withholds the trace line.
- **Logs on failure too.** If the downstream app raises before sending
  `http.response.start`, the line is still emitted (in a `finally`) with `status=None`,
  so a transport-layer fault is never silently unlogged.

Scope guard: transport-envelope logging only. No request/response *body* capture, no
general protocol debugger.

## Consequences

- New operator config surface `KDIVE_MCP_TRACE`; the generated config reference
  (`just config-docs`) must be regenerated and committed.
- When enabled, one log line per HTTP request — potentially voluminous under load. This
  is why it is off by default and documented as a debug-only aid, not normal-operation
  telemetry.
- `Mcp-Session-Id` values land wherever the logs land (journal, CI log). It is a session
  handle, not an auth secret, so this is acceptable and matches the issue's stated intent;
  documented as a residual.
- Middleware ordering is now load-bearing: the trace must stay outermost to keep full
  visibility. A future middleware that must run before it would require revisiting this.

## Considered & rejected

- **Do nothing.** The diagnostic gap is real and blocks reconstructing a session lifecycle
  (initialize → requests → 404 → re-initialize-or-not) from server logs, which is exactly
  what proving transport-layer client conformance requires. Rejected.
- **Reuse `KDIVE_LOG_LEVEL=debug` as the gate** (instead of a dedicated flag). Global DEBUG
  floods the logs with library internals (uvicorn, httpx, opentelemetry); an operator who
  wants only the transport trace should not have to accept that noise. A dedicated,
  single-purpose flag is a cleaner, cheaper opt-in. Rejected.
- **Extend the FastMCP tool-call middleware.** Structurally cannot see the transport
  (initialize, resources, session-layer 404) — the wrong layer for this. Rejected.
- **Expose uvicorn `access_log` control** (the issue's optional secondary facet). uvicorn's
  access log carries method/path/status but *not* the `Mcp-Session-Id` or
  `MCP-Protocol-Version`, so it cannot reconstruct a session lifecycle — the trace
  middleware supersedes it. Adding an `access_log` toggle nobody has asked to flip (the
  uvicorn default is fine) is a speculative config knob. Rejected as redundant and
  speculative; the primary acceptance criteria are met without it.
- **Full request-body capture / a general protocol debugger.** Exceeds the need and cuts
  against the no-speculative-features rule; a body may also carry secrets the envelope does
  not. Rejected by the scope guard.
- **Log at response completion (full stream duration) instead of at response start.** A
  long-lived SSE stream would delay or withhold the trace line for the duration of the
  stream; time-to-response-headers plus status is what the lifecycle-reconstruction use
  case needs. Rejected.
