# ADR 0126 — Synchronous-tool transport bound

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers

## Context

During MCP-surface testing a `systems.provision` call returned a raw "The socket connection was
closed unexpectedly" — a transport-level drop, not an envelope — and a retry succeeded.
`systems.provision` enqueues a job and returns fast
(`src/kdive/mcp/tools/lifecycle/systems/registrar.py:102-130`;
`src/kdive/services/systems/admission.py`), so the drop is most consistent with a synchronous
DB/libvirt call blocking the asyncio event loop in the request path long enough for the client
to time out. FastMCP runs over streamable HTTP (ADR-0010) with no per-tool request timeout
(`src/kdive/__main__.py:333`), so a stall surfaces as a dropped socket rather than a typed
error. See `../design/mcp-onboarding-error-ergonomics.md`.

## Decision

We will (1) audit the `systems.provision` request path for synchronous blocking calls and offload
them to `asyncio.to_thread` so one request cannot block the event loop, and (2) wrap synchronous
tool bodies with an execution-time bound that, on timeout or an otherwise-uncaught
transport-level failure, returns a `transport_failure` envelope (with ADR-0123's `detail`)
instead of letting the socket drop. The bound's threshold is set above the legitimate worst case,
determined by a reproduction spike.

## Consequences

- A stalled synchronous tool returns a typed, retryable envelope the caller can act on, instead
  of an opaque socket error.
- Offloading blocking calls removes head-of-line blocking, so a slow operation no longer stalls
  unrelated concurrent requests.
- New obligation: a reproduction spike to confirm the blocking call and choose a threshold that
  does not abort legitimate slow operations.
- This refines ADR-0010 (transport) and ADR-0019 (envelope completeness) without changing the
  job-enqueue model — fast tools stay synchronous.

## Alternatives considered

- **Turn `systems.provision` into a polled async job**: it already enqueues and returns; the
  problem is event-loop blocking in the fast path, not long work in the handler — rejected as
  solving the wrong problem.
- **Raise the client/proxy socket timeout**: hides the stall instead of converting it to a typed
  error and does not fix the head-of-line blocking; rejected.
- **A timeout only, no offload audit**: would convert the drop to an envelope but leave concurrent
  requests stalled behind the blocking call; rejected as half a fix.
