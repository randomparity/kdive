# 0863 — Offload remote console redaction after probing

## Status

Accepted (2026-11-24)

## Context

The strict reader's assembled bytes can be large. Its redactor runs on the event loop,
although the boot snapshotter already offloads the same operation. ADR-0429 requires a fresh
redactor, propagated failures, a stable cursor and a leader-liveness flag.

## Decision

Await asyncio.to_thread(self._redact, data) after the existing awaited leader probe.
Return those bytes with the original next_index and pumped values. Keep redaction mandatory
even for an empty window. Registry access uses SecretRegistry's existing locked snapshot.

## Consequences

Redaction yields the event loop, with executor scheduling overhead. Cancellation can leave
read-only thread work running; it cannot produce an unredacted successful response or write.
The probe still precedes redactor construction, preserving secrets registered during probing.
This makes no latency or globally atomic registry-snapshot guarantee.

## Considered & rejected

- **Keep synchronous redaction.** verified: the actual reader/probe/redactor reproduction in
  the local task-155 proof at source 67167c3b1 fails its off-event-loop assertion.
- **Redact with part assembly before probing.** judgment: this moves the registry snapshot
  earlier and loses the existing opportunity to mask values registered during the probe.
- **Add a process pool or redactor service.** judgment: the snapshotter already uses the
  standard thread executor; a new lifecycle and serialization boundary is unnecessary here.
