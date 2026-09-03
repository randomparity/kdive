# 0584 — Provider-host authority fences external-boot mutations

## Status

Accepted (2026-08-28)

## Context

ADR-0583 requires every external-boot definition, module-tree, attachment, power, recovery, and
cleanup mutation to validate current authority at its commit point. It also requires takeover to
wait for positive quiescence: after takeover is acknowledged, no older actor may publish, boot,
restore, delete, or commit stale core truth.

The existing System advisory lock cannot provide that guarantee. It ends with its database
transaction, while a provider call can continue after commit, connection loss, or worker
replacement. Credential-bound job attempts prevent stale database writes, but they cannot revoke a
libvirt operation already admitted to a provider host. Libvirt itself has no KDIVE generation field
on which an old call can be rejected.

Two deployment shapes can close the gap:

1. Extend the worker and reconciler. They can allocate and check a database generation around each
   call, but a replaced process can still reach libvirt after losing database authority. Holding a
   session lock over provider work detects process or connection loss; it does not prove the remote
   mutation stopped before a successor begins.
2. Put a small authority beside each provider mutation endpoint. It can be the sole principal able
   to mutate the owned provider objects, serialize their commit points, journal results, and delay a
   takeover acknowledgement until every older admitted operation has ended. This adds a deployment
   role and protocol, but places the fence at the boundary that commits the mutation.

The second shape is the smallest one that makes the required takeover outcome falsifiable. This
decision calls that role the **provider-host authority**. It is a narrow mutation broker, not a job
worker, scheduler, reconciler, or source of lifecycle truth.

## Decision

External-boot provider mutations use protocol `external-boot-authority-v1`. Core allocates a
monotonic authority generation under the System lock; the provider-host authority authenticates and
enforces it at each provider commit point; core accepts results only while the same generation
remains current.

### Durable authority

Postgres stores one current authority row per System. A generation is a positive 64-bit integer
allocated only by a security-definer database function while holding the System transaction lock.
That function authenticates the worker-incarnation credential and verifies the exact running job
attempt before allocation. The generation is never supplied by a caller and never reused. The row
binds:

- System, Allocation, activation, Run, plan, and operation-attempt identities;
- purpose: `activate`, `recover`, `resolve-conflict`, `release`, or `teardown`;
- the authenticated worker incarnation that requested the generation;
- generation state: `allocating`, `current`, `superseded`, or `retired`;
- provider kind, provider authority instance, creation time, and acknowledgement time.

Allocation first inserts `allocating` and supersedes the prior current generation in one
transaction. The generation does not authorize mutation until the matching provider-host authority
has acknowledged it. Core then changes it to `current` only when that acknowledgement names the
same immutable binding. Failure or ambiguity leaves it non-current and permits no provider or core
write.

The authority reference passed through the provider seam is an opaque, bounded identifier for this
row. Possessing it is not authority. The database and provider-host authority independently verify
the complete binding and the authenticated worker incarnation. Existing incarnation credentials
remain the worker identity; this protocol does not mint a second standing worker credential.

### Provider-host authority and access boundary

Every provider endpoint that advertises external-boot v1 runs one authority instance for its owned
mutation scope. Local-libvirt and remote-libvirt use the same protocol even when the local instance
is colocated with a worker. The authority is the only KDIVE principal permitted to mutate the
provider objects covered by this protocol. Workers and the reconciler retain read-only observation
access and cannot bypass it through a libvirt socket, SSH account, filesystem permission, helper,
or service credential. Deployment validation fails closed when that exclusivity is not configured.

Requests use mutually authenticated transport and carry the opaque authority reference, immutable
operation identity, expected source identity, requested target identity, and operation digest. The
authority authenticates its peer as a registered active worker incarnation, resolves the reference
through its least-privilege database role, and requires the peer, System, activation, attempt,
purpose, provider kind, and authority instance to match. Caller commands, paths, credentials, and
provider-native definitions are not accepted through the shared protocol.

The authority database role may read authority bindings and append authority acknowledgements and
journal-head checkpoints. It cannot allocate generations or advance Run, System, activation, job,
or accounting state. The core role may allocate generations and commit lifecycle truth but cannot
forge a provider acknowledgement. Provider credentials and mutation-capable sockets are available
only to the authority process.

### Positive quiescence and takeover

The authority has one serialized mutation lane per System. Before it acknowledges generation `G`,
it durably installs `G` as the lane watermark and prevents admission of every lower generation. It
then waits for every already-admitted lower-generation operation to reach one of these observed
terminal conditions:

- no provider mutation began;
- the provider call returned and the resulting provider state was observed;
- the call outcome was lost, but repeated observation resolved the state to the recorded source or
  target identity; or
- observation proved a third, mixed, unreadable, or unowned state, which is journaled as conflict.

An unanswered, cancellable, timed-out, disconnected, or merely presumed-dead call is not quiescent.
The authority never acknowledges takeover while such a call can still commit. It remains responsible
for observing that call after client disconnect or process restart. An authority instance that
cannot restore its journal and prove the lane has no older admitted operation refuses service.

After quiescence, the authority fsyncs the watermark, the prior operation outcomes, and its
acknowledgement before returning it. The acknowledgement binds the authority instance, System,
generation, operation binding, journal sequence, and a digest of the quiescence evidence. Core
records the acknowledgement before marking the generation current. Once returned, every request
from a lower generation is rejected before provider access, including a retry carrying a previously
successful idempotency key.

If the provider API cannot expose completion of an in-flight mutation, the authority must serialize
by owning and waiting for the actual call. A deployment cannot substitute a lease timeout or worker
heartbeat. If an authority process can die while a provider call survives it, its supervisor must
keep the same journal and execution owner alive until the call ends; otherwise that provider cannot
advertise external-boot v1.

### Mutation journal and stable ownership

The authority journal is append-only and crash-recoverable. Each record binds authority instance,
System, activation, generation, operation identity, attempt, purpose, request digest, expected
source identity, intended target identity, recovery-object identities, phase, provider observation,
and previous-record digest. Phases are `admitted`, `mutation-started`, `provider-returned`,
`observed`, and `terminal`. Journal sequence and digest prevent deletion, replacement, or reordering
within the retained sequence from being accepted as continuity.

Postgres separately stores the exact trusted journal head for each authority lane: authority
instance, System, sequence, record digest, phase, and operation identity. The authority fsyncs a
record, then advances that head with a monotonic compare-and-set whose expected value is the
record's previous sequence and digest. It must anchor `admitted` and `mutation-started` before any
provider access. Later phases are likewise anchored before their evidence can authorize another
provider commit, a takeover acknowledgement, or a core lifecycle result. The database head is not
a substitute journal and carries no provider definitions or output.

On restart, the local journal must end at exactly the trusted database head. A shorter journal,
including a valid-prefix truncation, a longer uncommitted suffix, or any sequence/digest/identity
divergence refuses service and cannot acknowledge takeover. This availability cost closes the case
where a surviving provider call appears only in a lost suffix: its `mutation-started` record and
trusted head were committed before the call began, so losing that record is observable. Repair is
an audited platform-operator action that restores the exact retained journal bytes; it never moves
the trusted head backward, declares an operation absent, or authorizes provider access.

Preparation may create only private, discardable objects before current authority is checked.
Publishing a module tree, recovery object, persistent definition, attachment, power transition, or
deletion requires a fresh generation check immediately before that provider commit point and a
journal record immediately after observation. One operation can have several commit points; losing
authority between them stops before the next one and leaves the successor to classify the recorded
partial state under ADR-0583.

Recovery objects retain the stable `(System, activation, recovery reference)` ownership assigned at
preparation. Takeover changes generation and actor, never ownership. A successor may resume or
delete one only when the journal and provider observation prove that stable binding. Teardown uses
a newer
`teardown` generation and the narrower deletion authority from ADR-0583; it may destroy the owned
System without intact recovery evidence, but unproven recovery objects remain quarantined.

The journal is not lifecycle truth. Postgres remains the source of activation and job state. Journal
evidence tells a current actor what provider mutation may have happened and supports audit and
reconciliation.

### Core result fencing

Every actor-originated activation transition, attempt or deadline update, job result, failure,
recovery completion, audit result, and `cleanup_complete` write calls one credential-bound database
function. The function locks the System and requires all of:

- the worker incarnation credential is active;
- the exact job and attempt remain owned by that incarnation;
- the exact authority generation is `current` and has the expected binding;
- its recorded provider acknowledgement matches the authority instance, journal sequence, and
  operation digest supplied with the result; and
- the requested lifecycle edge is legal.

A mismatch affects zero lifecycle, job, cleanup, or result rows and returns `superseded`. The stale
actor may emit a bounded local diagnostic, but cannot append a durable audit result as though it
were current. The accepted current actor records the supersession and takeover trail instead.

Read-only observations do not require mutation authority, but an observation cannot authorize a
later write. Reconciliation, redelivery, conflict resolution, release, teardown, and a later Run
each allocate a distinct newer generation before mutation.

### Audit, retention, and failure behavior

Core audit records retain the allocating and replacing principal, worker incarnation, complete
authority binding, prior and new generations, purpose, acknowledgement identity, and outcome. The
authority journal retains peer identity, request digest, commit-point observations, conflicts, and
quiescence evidence. Neither surface records credentials, provider secrets, raw definitions, or
unbounded provider output. Operator access to either trail is project-scoped and uses the existing
audit authorization boundary; only platform operators may inspect authority-instance diagnostics.

Authority rows and journals remain until the activation is terminal, cleanup is verified, every
recovery object is absent or quarantined, and the existing audit-retention floor has elapsed.
Unreachable authority, journal corruption, acknowledgement mismatch, or unreadable provider state
fails closed. Ordinary activation or recovery remains pending or enters `recovery_conflict` as
ADR-0583 specifies. No timeout promotes authority, assumes quiescence, recaptures a baseline, or
deletes recovery evidence.

A privileged host administrator can bypass the authority and remains outside the protocol. Such
interference is detected as an unexpected provider identity and enters conflict; it is not described
as fenced or recoverable by generation alone.

### Required proofs

The implementation must provide provider-neutral contract tests and live provider proofs that:

- race two allocations and obtain strictly ordered, never-reused generations;
- reject caller-selected, cross-System, cross-Run, cross-activation, cross-attempt, wrong-purpose,
  wrong-provider, wrong-authority-instance, and inactive-worker references;
- pause an old actor before and after every provider commit point, lose each response, acknowledge a
  successor, then prove the old actor cannot publish, boot, restore, delete, or commit core truth;
- withhold acknowledgement while an old provider call is unresolved, including across authority
  restart, and acknowledge only after journal recovery and positive observation;
- resume target, mixed, source, conflict, release, and teardown paths without changing stable
  recovery ownership or capturing a new baseline;
- deny a stale actor from an earlier completed Run after a later Run obtains authority;
- reject missing, reordered, truncated, corrupted, or foreign journal records and authority
  acknowledgements, including a journal truncated to a valid prefix after `mutation-started` while
  its provider call survives, and withhold takeover until the exact trusted head is restored; and
- prove deployment ACLs deny workers and reconcilers direct provider mutation while the authority
  can perform only its configured provider scope.

The native x86_64 and ppc64le live tiers exercise the same protocol. A provider lacking a way to
place every external-boot commit point behind the authority or to preserve unresolved execution
across authority restart does not advertise external-boot v1.

## Consequences

- External boot gains a fence at the provider mutation boundary and a separate database fence for
  core truth. Neither is treated as a substitute for the other.
- Local and remote libvirt need a provider-host authority deployment and must remove direct worker
  mutation access for external-boot objects. Colocation is allowed; bypass is not.
- Takeover availability is bounded by the oldest unresolved provider call. The protocol prefers a
  visible stalled takeover over two actors that can mutate concurrently.
- The durable journal and authority rows add storage and operational diagnostics, but make lost
  responses and restarts classifiable without recapturing source state.
- Existing non-external install, boot, control, and capture paths are unchanged. Moving another
  mutation family behind this authority requires a later decision.
- Accounted cleanup is now evidenced end to end. The provider adapter receives the `sequence` and
  digest of the `mutation-started` record this authority anchored for that same mutation, as the
  service-constructed `AuthorityCommitContextV1` that [ADR-0591](0591-authority-commit-context-carries-the-anchored-journal-proof.md)
  adds to the `commit` seam. The local adapter's `cleanup` commit point builds its finalization
  proof from that context, so the tombstone ADR-0586 retains is discharged against this journal
  rather than against an assertion. The `teardown` commit point still publishes a tombstone with no
  finalizer; that remains open and belongs to the local adapter.

## Considered & rejected

- **Extend only the worker and reconciler.** Their database generation can deny stale core commits,
  but cannot stop an already-admitted libvirt call after the worker loses its claim. A session lock
  reports connection loss; it does not prove provider quiescence.
- **Lease authority by heartbeat or deadline.** Expiry makes a successor eligible while an old
  provider call may still commit. A lease is liveness evidence, not positive quiescence.
- **Hold a database session lock throughout provider work.** Loss releases the lock precisely when
  the outcome is least certain. A successor could acquire it before the remote mutation ends.
- **Trust idempotency keys without a generation watermark.** They deduplicate one operation but do
  not revoke a different old operation or prevent a later stale retry.
- **Let each provider implement an unrelated fence.** Providers need different commit adapters, but
  unrelated authority semantics would make core result fencing and cross-provider adversarial tests
  non-portable.
- **Make the provider-host authority the lifecycle source of truth.** It would duplicate the core
  state machine and turn journal recovery into distributed consensus. The authority owns mutation
  serialization and evidence only; Postgres owns lifecycle truth.

## Implementation ownership

Implementation is deliberately excluded from this ADR change. PR-sized sub-issues under epic #2105
own database authority and result fencing, the provider-host authority protocol and journal, and
deployment ACL plus adversarial/live proofs.
