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
records an operation-unique deterministic object key before PUT, records the returned object
identity after PUT,
and atomically commits artifact-row registration, audit, and the operation's published outcome
under the Run lock. Every transition revalidates the credential-bound worker, exact job attempt,
current-operation link, and nonterminal job state.

Provider execution and publication are orthogonal monotonic states on the same row. ADR-0558's
operation state still advances to terminal `exited` only after positive process and provider
quiescence. Publication advances `pending -> publishing -> published | canceling -> discarded`;
`published` and `discarded` are terminal. A committed artifact row is required for `published`.
An absent journaled object is required for `discarded`. Claim, cancellation acknowledgment,
retry, and reclamation all require the product state `(exited, published|discarded)`; an operation
in any other product state remains current and recoverable.

Each operation key contains the durable operation id rather than only the job id. The capture PUT
uses atomic conditional creation (`If-None-Match: *`) and carries the operation id as object
metadata. Recovery never resumes that PUT. A cancellation owner arbitrates an ambiguous PUT by
conditionally creating a zero-byte tombstone with the same operation metadata at the same key.
Exactly one conditional create can win. If the capture object won, recovery HEADs and deletes its
immutable version, then wins the tombstone create; if the tombstone won, the delayed capture PUT
fails its precondition. Recovery verifies the tombstone identity, then deletes its version after
the sole capture PUT has resolved. Only that completed arbitration proves the key absent and no
request capable of recreating it. Artifact rows continue to expose only their opaque id; the key
and tombstone shape are not an MCP contract.

Session-loss handling runs inside the fence owner: the authority monitor cancels the publisher
before any further database transition and starts draining any in-process PUT. The released job
fence alone does not authorize another actor to prove absence. Under a fresh connection the owner,
or a lifecycle-authorized replacement, takes that fence and the Run lock, revalidates the exact
attempt, and moves `pending|publishing` to `canceling`. That is the database linearization point
after which publication commit is refused. Outside the Run lock it completes the conditional-
create arbitration above; this remains safe even when the old PUT has not drained. It then
reacquires the Run lock to record `discarded`. An external cancellation request waits for the
live owner's job fence but may only request `canceling`; it cannot record `discarded` without
arbitration. Its cancellation linearizes only after fence acquisition, so publication already
committed while the live owner held the fence remains `published`. Failure to prove either
terminal outcome leaves the operation recoverable and bars acknowledgment, retry, and reaping.

Migration 0113 raises the worker fence protocol from 3 to 4, adds publication state to supervised
operations, and augments ADR-0558's singleton cutoff with `publication_closed` and `complete`.
Under the capture-protocol fence it rejects every protocol-3-or-older incarnation not positively
terminated by its lifecycle authority, rejects every running capture job or nonterminal operation,
installs protocol 4 in registration, authentication, and capture claim, and rechecks that
population before setting both cutoff fields atomically. Deployment uses the existing offline
Compose, local-host, and Kubernetes lifecycle authorities to stop old workers before migration;
no rolling compatibility path exists. Historical reclamation may use the cutoff only when
`complete` is true. Attempt-linked reclamation requires the product state
`(exited, published|discarded)`.

## Consequences

- The job fence covers a potentially slow PUT, but the Run lock remains limited to short database
  checks and the metadata commit.
- A worker crash can leave a journaled key or stored object, but conditional-create arbitration
  lets startup recovery settle an ambiguous PUT, adopt an already committed row, or remove the
  exact unregistered version. It never guesses from object age or a timeout.
- A failed delete retains a nonterminal operation and blocks retry and reaping until recovery can
  prove absence. This prefers retained residue to publication after reclamation.
- Concurrent attempts have distinct operation keys, and job claiming remains barred until prior
  publication is terminal. Existing row/etag reconciliation remains defensive for pre-cutover
  data, not the concurrency mechanism.
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
- **Wait for a request timeout, then trust absence.** Client timeout does not prove the store
  rejected a request already in flight. Conditional create at one unique key supplies the needed
  storage-side serialization point without a timing assumption.
- **Do nothing after ADR-0558.** Provider quiescence says nothing about the handler's later PUT and
  metadata transaction, so it cannot authorize reaping.
