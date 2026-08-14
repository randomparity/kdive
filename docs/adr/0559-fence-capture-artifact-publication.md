# ADR-0559: Fence capture artifact publication

## Status

Accepted

## Context

ADR-0558 gives each `capture_traffic` job attempt a durable provider-operation identity and
positively proves provider exit. The handler then returns from that supervised boundary and
publishes the pcap through an object-store PUT followed by artifact-row registration. The PUT is
intentionally outside the Run advisory lock, so provider exit alone does not prove that a delayed
or canceled attempt can no longer publish. A later reaper could remove provider state while that
attempt still commits an object or metadata row.

Publication crosses PostgreSQL and an object store, which cannot share one transaction. The
ordering must therefore make incomplete work discoverable and compensatable without holding the
Run lock across object-store I/O.

## Decision

The supervised capture operation owns publication as a durable phase. Its session-level per-job
fence remains held from provider launch until publication reaches a terminal outcome. The worker
records the deterministic object key before PUT, records the returned object identity after PUT,
and atomically commits artifact-row registration, audit, and the operation's published outcome
under the Run lock. Every transition revalidates the credential-bound worker, exact job attempt,
current-operation link, and nonterminal job state.

The operation state distinguishes provider mutation from publication. Positive provider-exit and
quiescence evidence advances `running` to `publishing`; only a committed artifact row advances it
to terminal `published`. A no-artifact outcome advances it to terminal `discarded` only after the
deterministic key is absent or an artifact row already owns it. A later attempt cannot be claimed
while the prior operation is nonterminal.

Cancellation, session loss, or replacement recovery cancels any in-process PUT, waits for the
blocking store call to return, and then checks the key under the Run lock. If no artifact row owns
the key, it deletes the object and verifies absence before recording `discarded`. If a row was
already committed, publication is `published` and cancellation cannot rewrite it. Failure to
prove either terminal outcome leaves the operation recoverable and bars retry and reaping.

Migration 0113 adds publication state to supervised operations and augments ADR-0558's singleton
cutoff with `publication_closed` and `complete`. The migration holds the capture-protocol fence,
requires the existing operation-quiescent cutoff, proves no legacy producer can rejoin, and sets
both fields atomically. Historical reclamation may use the cutoff only when `complete` is true.
Attempt-linked reclamation requires an exited operation with terminal publication evidence.

## Consequences

- The job fence covers a potentially slow PUT, but the Run lock remains limited to short database
  checks and the metadata commit.
- A worker crash can leave a journaled key or stored object, but startup recovery has enough
  durable identity to finish registration only when already committed or remove the unregistered
  object. It never guesses from object age.
- A failed delete retains a nonterminal operation and blocks retry and reaping until recovery can
  prove absence. This prefers retained residue to publication after reclamation.
- Concurrent attempts cannot PUT the same key because job claiming remains barred until prior
  publication is terminal. Existing row/etag reconciliation remains defensive for pre-cutover or
  externally repaired data, not the concurrency mechanism.
- The MCP contract and artifact contents do not change.

## Considered & rejected

- **Keep cancellation compensation only in the handler.** An in-memory task cannot survive worker
  death and provider exit is acknowledged before compensation finishes, so later reaping lacks a
  durable barrier.
- **Hold the Run lock across PUT.** This serializes unrelated Run operations on object-store
  latency and reverses ADR-0519 without being necessary; the job fence plus durable journal closes
  the race.
- **Write the artifact row before PUT.** A crash leaves a misleading row naming absent or partial
  bytes, and rollback still requires cross-system recovery.
- **Use a unique object key per attempt and garbage-collect later.** It avoids overwrite races but
  deliberately creates orphan objects and moves correctness to an age-based sweep.
- **Do nothing after ADR-0558.** Provider quiescence says nothing about the handler's later PUT and
  metadata transaction, so it cannot authorize reaping.
