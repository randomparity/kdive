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
them to `asyncio.to_thread` so one request cannot block the event loop, and (2) bound the
**pre-mutation segment** of the request path (validation, admission checks, lock acquisition) with
an execution-time timeout that returns a `transport_failure` envelope (with ADR-0123's `detail`)
instead of letting the socket drop. The timeout covers only the pre-mutation segment because
Python cannot kill a running thread — `asyncio.wait_for` over a `to_thread` future abandons the
future while the thread completes — so a timeout fired *after* a mutation began would let the
mutation land while the caller is told it failed, and since `transport_failure` is
`retryable=True` the caller would auto-retry and double-provision. Once the first mutation begins
the request runs to its own completion and returns its real envelope. As a backstop for a genuine
transport drop mid-request, `systems.provision`/`define` are made idempotent via the existing
idempotency ledger (ADR-0016) so a retried identical request is deduped. The segment boundary and
threshold are confirmed by a reproduction spike.

## Consequences

- A stall in the pre-mutation segment returns a typed, retryable envelope the caller can act on,
  instead of an opaque socket error — without the timeout ever abandoning an in-flight mutation.
- Offloading blocking calls removes head-of-line blocking, so a slow operation no longer stalls
  unrelated concurrent requests.
- The retry path is safe: a `transport_failure` retry is deduped by the idempotency ledger, so the
  retryable classification does not cause duplicate Systems/allocations/jobs.
- New obligations: a reproduction spike to confirm the blocking call sits in the pre-mutation
  segment and to choose a threshold that does not abort legitimate slow pre-mutation work; and, if
  the idempotency ledger does not already cover the provision/define path, extending it.
- This refines ADR-0010 (transport) and ADR-0019 (envelope completeness) without changing the
  job-enqueue model — fast tools stay synchronous.

## Alternatives considered

- **Wrap the whole tool body in the timeout**: simplest, but a timeout firing after the mutation
  began abandons a `to_thread` that completes anyway, so the caller sees `transport_failure`,
  auto-retries (retryable), and double-provisions — rejected for the segmented bound + idempotency
  backstop.
- **Turn `systems.provision` into a polled async job**: it already enqueues and returns; the
  problem is event-loop blocking in the fast path, not long work in the handler — rejected as
  solving the wrong problem.
- **Raise the client/proxy socket timeout**: hides the stall instead of converting it to a typed
  error and does not fix the head-of-line blocking; rejected.
- **A timeout only, no offload audit**: would convert the drop to an envelope but leave concurrent
  requests stalled behind the blocking call; rejected as half a fix.
