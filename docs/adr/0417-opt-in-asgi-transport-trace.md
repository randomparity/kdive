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
- **One structured line per request, emitted at response headers.** The single INFO record
  is emitted when `http.response.start` is observed — at the response headers, *not* at
  stream close — via the dedicated `kdive.mcp.transport_trace` logger, carrying: `method`,
  `path`, `mcp_session_id` (value) + `mcp_session_id_present`, `mcp_protocol_version`,
  `status`, and `duration_ms`. An `emitted` flag guards exactly-once: a `finally` emits the
  line **only** if `http.response.start` was never seen (the failure path below). Emitting
  at response-start rather than in the `finally` is deliberate — for a long-lived SSE
  response `await self.app(...)` returns only at stream close, so a finally-only emission
  would withhold the line for the whole stream and defeat real-time lifecycle observation.
- **Level-independent.** The trace logger is given its **own explicit `INFO` level**
  (`logger.setLevel(logging.INFO)`), so it emits independently of the root floor
  `KDIVE_LOG_LEVEL` sets (`facade.py`): the `KDIVE_MCP_TRACE` flag — not the global log
  level — is the gate. Without the explicit level, a deployment running
  `KDIVE_LOG_LEVEL=warning` would silently drop every trace line even with the flag on,
  because a child logger otherwise inherits the raised root floor. This depends on the OTel
  bridge handler `facade.py` installs on the root logger staying at `NOTSET`; a regression
  guard asserts that.
- **Redaction-safe.** The `Authorization` header is logged as `authorization_present`
  (bool) only — never the value, honoring the mandatory-redaction invariant. The
  `Mcp-Session-Id` is a session handle (not an auth credential) and is logged as its
  value. As defense-in-depth the record still passes the existing logging redaction floor
  (ADR-0090), so a token that somehow reached a logged field is scrubbed.
- **Duration = time to response headers.** Measured from request receipt to the
  `http.response.start` emission (time-to-first-byte), so a long-lived SSE stream never
  delays or withholds the trace line.
- **Logs on failure too, exactly once.** When the downstream app raises or is cancelled
  before sending `http.response.start`, the `finally` emits the line (the `emitted` flag is
  still false) with `status=None`, then the exception/cancellation re-raises (never
  swallowed), so a transport-layer fault is never silently unlogged. `duration_ms` is
  computed unconditionally in the `finally` (`monotonic()` minus request-receipt), *not* in
  an `except` handler — so it is a real number even for `asyncio.CancelledError` (a
  `BaseException`, the ordinary SSE client-disconnect path) that an `except Exception` would
  miss. The `emitted` flag makes a request produce exactly one trace line whether it
  succeeds or fails.

Scope guard: transport-envelope logging only. No request/response *body* capture, no
general protocol debugger.

## Consequences

- New operator config surface `KDIVE_MCP_TRACE`; the generated config reference
  (`just config-docs`) must be regenerated and committed.
- When enabled, one log line per HTTP request — potentially voluminous under load. This
  is why it is off by default and documented as a debug-only aid, not normal-operation
  telemetry.
- **Restart/observer effect.** The config registry snapshots `os.environ` at process start,
  and the gate is applied at server boot, so enabling `KDIVE_MCP_TRACE` takes effect only
  after a server restart. A restart drops every in-memory MCP session, so the trace captures
  *newly-established* sessions, not the one currently wedged — and each existing client's
  next request returns exactly the `404 Session not found` this work exists to study. The
  restart requirement is inherent to the boot-time env snapshot, not to the build-time
  add-vs-branch choice (a per-request branch would read the same boot snapshot); it is
  disclosed here so an operator arms the flag *before* reproducing an incident rather than
  expecting to attach to a live one.
- `Mcp-Session-Id` values land wherever the logs land (journal, CI log). In the MCP
  streamable-HTTP transport the session id identifies a session but is **not** an
  authorization credential — authorization is the separate `Authorization: Bearer` token,
  which is logged presence-only — so a logged session id is not a resumption/hijack
  capability on its own. It remains a stable cross-user correlator in shared logs; that is
  the accepted residual, and it matches the issue's explicit intent to log the value so a
  server line can be correlated to a specific client session.
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
- **Log a salted hash of `Mcp-Session-Id` (or presence-only) instead of the raw value.** A
  hash would give per-session correlation without disclosing the handle in shared logs. But
  the issue's acceptance criteria require the *value* ("whether an `Mcp-Session-Id` was
  present and its value"), because the operator reconstructs the lifecycle by correlating a
  server trace line to the id the *client* logged; a server-only salted hash cannot be
  matched against a client's raw id. Since the value is not an auth credential (above),
  logging it meets the need. Rejected against the stated criteria; presence-only is kept as
  a companion field, not a replacement.
- **Emit the transport envelope as an OpenTelemetry span** via the existing tracer
  (`facade.py`) with method/path/status/duration and the two MCP headers as attributes.
  This would inherit sampling (bounding the "voluminous under load" consequence),
  correlation, and the redaction floor. Rejected as the primary mechanism: it routes the
  trace to the OTLP collector, which many debug deployments do not run, whereas the
  motivating use case wants the lifecycle in the *plain server log/journal* an operator is
  already reading; it also needs ASGI span plumbing and is a heavier opt-in than a single
  log flag. The bespoke INFO logger keeps the operator path to "set flag, read journal."
- **Full request-body capture / a general protocol debugger.** Exceeds the need and cuts
  against the no-speculative-features rule; a body may also carry secrets the envelope does
  not. Rejected by the scope guard.
- **Log at response completion (full stream duration) instead of at response start.** A
  long-lived SSE stream would delay or withhold the trace line for the duration of the
  stream; time-to-response-headers plus status is what the lifecycle-reconstruction use
  case needs. Rejected.
