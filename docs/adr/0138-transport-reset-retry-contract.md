# ADR 0138 — Transport-reset retry contract for long-polls

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

During black-box MCP testing (D3) four calls (#18, #27, #29, #38) failed with a raw
`socket connection was closed unexpectedly` message — a `fetch()`-level transport drop,
**not** a kdive `ToolResponse` envelope with `error_category`/`retryable`. All four
succeeded on a bare retry. A client cannot tell "transient, retry me" from a real failure
when the failure is a raw socket error, so it cannot follow the retryable contract the
envelope was built to advertise (ADR-0118).

The drops correlate with the long-poll tools. `jobs.wait` holds a single streamable-HTTP
POST open for up to `MAX_WAIT_S = 300.0` seconds while it polls
(`src/kdive/mcp/tools/catalog/jobs.py`). A 300 s held stream is a plausible reset window:
idle reverse proxies and load balancers apply their own read/idle timeout, and FastMCP's
uvicorn server imposes **no** per-request duration cap on an in-flight streaming POST
(`timeout_keep_alive` governs idle time *between* requests, not the life of an in-flight
one — confirmed in the M0 skeleton design note,
`docs/archive/superpowers/specs/2026-06-03-mcp-skeleton-auth-jobs-design.md`).

Once an intermediary severs an in-flight stream, the response bytes never arrive, so kdive
**cannot** wrap that specific drop in an application envelope — the seam that would emit it
is on the far side of a closed connection. This bounds what is honestly fixable here. The
distinct, in-process synchronous-call stall — a fast tool blocking the event loop until the
client times out — is a separate failure mode owned by ADR-0126 (#481); this ADR does not
touch the dispatch boundary.

## Decision

The **documented retry contract is the primary mitigation**; the two code changes are
supporting, in-control adjustments. Neither code change alone resolves the raw in-flight drop
(unwrappable, per Context).

1. **Set an explicit uvicorn keepalive on the server transport.** `_run_server` passes
   `uvicorn_config={"timeout_keep_alive": _HTTP_KEEPALIVE_S}` (65 s) to
   `app.run_async(transport="http", …)`. `timeout_keep_alive` governs only how long the server
   holds an *idle keepalive TCP connection* open *between* requests on that connection, so its
   benefit is **conditional**: it reduces reconnect churn only for a client that **reuses** the
   same TCP connection across successive short polls. It does **nothing** for a client that opens
   a fresh connection per call, and it does **not** extend the in-flight hold of a single long
   `jobs.wait` — no uvicorn knob does — and we do not claim it does. `_HTTP_KEEPALIVE_S` (65 s)
   sits below `MAX_WAIT_S` (300 s) by design: it bounds the between-poll idle gap, not the
   in-flight hold. This is best-effort churn reduction, not the fix.

2. **Keep `jobs.wait` returning promptly at its cap as a "still running, call again"
   signal, and make that contract explicit.** A non-terminal `jobs.wait` already returns
   the job's current (`running`/`queued`) envelope with `jobs.wait` in
   `suggested_next_actions` — a bounded poll the agent re-issues, not a held idle stream.
   The default `timeout_s` stays 30 s (well under any normal proxy timeout); the 300 s
   **cap** stays opt-in for callers that knowingly accept the reset risk. We add a test that
   pins the prompt non-terminal return so a future change cannot silently hold the stream to
   the deadline.

3. **Document the retry contract.** The guide states plainly that long-poll/transport resets
   are transient, that idempotent reads and `jobs.wait` are safe to retry, and that the
   token-efficient pattern is repeated short `jobs.wait` calls (default 30 s) rather than one
   300 s hold. `transport_failure` is already `retryable=true` (ADR-0118), so a
   server-*observable* recoverable stream error already returns a categorized retryable
   envelope; the gap this ADR closes is the un-observable raw drop, closed by the documented
   contract (with the keepalive as best-effort churn reduction), not by inventing a new envelope
   path.

`_HTTP_KEEPALIVE_S` is a module constant, not a `KDIVE_*` setting: it is a deployment-coupling
default (sized against proxy norms), not per-run agent input, and no current user needs to
tune it (no speculative config).

## Consequences

- A client that follows the documented contract (short `jobs.wait`, retry on transport drop)
  no longer holds an idle stream long enough to be reset under normal proxy timeouts, and
  knows a raw socket close is transient rather than a real error.
- The explicit keepalive keeps the connection alive across the gaps between short polls,
  reducing reconnect churn; it is honest about not governing in-flight holds.
- A caller that explicitly requests a 300 s wait still risks an intermediary reset; the
  contract tells it that drop is retryable. We do not silently lower the cap and surprise a
  caller that opted into a long wait.
- No schema, migration, new MCP tool, env var, or envelope-shape change. `MAX_WAIT_S`, the
  default `timeout_s`, and the `transport_failure` taxonomy are unchanged.
- Refines ADR-0010 (transport) and ADR-0118 (retryable). Disjoint from ADR-0126, which bounds
  the in-process synchronous-stall path at the dispatch boundary.

## Considered & rejected

- **Lower `MAX_WAIT_S` to, say, 60 s.** Caps a caller that knowingly opted into a long wait,
  and still does not survive a proxy with a sub-60 s idle timeout. The contract (short default
  + retry-on-drop) covers the common case without removing the opt-in. Rejected.
- **Raise/tune uvicorn so the in-flight 300 s stream survives any proxy.** No uvicorn knob
  governs in-flight stream lifetime; the cutter is the intermediary, outside this process.
  Claiming a keepalive value "fixes" the hold would be the phantom-feature failure mode.
  Rejected.
- **Server-push the drop as a `transport_failure` envelope.** Once the in-flight stream is
  severed the bytes never arrive; there is no live connection to emit an envelope over.
  Rejected as not physically possible for the reported drop.
- **Add a `KDIVE_HTTP_KEEPALIVE_S` setting.** No user needs to tune it today; it is a
  deployment default sized against proxy norms. A module constant is the no-speculative-config
  choice; promote it to a setting if a real deployment needs a different value. Rejected for now.
- **Make `jobs.wait` itself retry across a transport drop.** The drop happens in the client's
  transport, below the tool; the tool never sees it. Client-side retry is the only place the
  contract can live. Rejected.
