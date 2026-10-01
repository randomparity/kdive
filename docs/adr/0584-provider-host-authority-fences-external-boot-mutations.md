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

### Amendment (2026-09-25): local-libvirt observes a running domain's modules as last published (#2785)

This amendment qualifies the observation rule in this section for local-libvirt. Local-libvirt
reads the guest module tree with libguestfs, which cannot open the disk of a running domain. So
after a successful activation, when the target runs, the tree was unreadable and the observation was
`unreadable`, and core refused the activate commit. When a module publication completes, the
provider now observes the published tree while the domain is still inactive and records that
observation in its recovery metadata. While the domain is active, the observation reports the
recorded value. While the domain is inactive, the observation still reads the tree. The recorded
value covers only provider mutations. A change that the running guest makes to its own module tree
is not a provider mutation, and the next inactive observation sees that change.

### Amendment (2026-09-28): an unanchored final record is retracted (#2793)

This amendment narrows the "longer uncommitted suffix" case in *Mutation journal and stable
ownership*. A record the trusted head never accepted records no provider mutation that the head
does not already show: `admitted` and `mutation-started` are anchored before provider access, so an
unanchored record either precedes that access or follows a `mutation-started` head that already
marks the operation unresolved. The authority therefore removes exactly one unanchored final
record, in two cases and in no others:

- at runtime, the record `_anchor` has just appended, when the head advance returns a definitive
  `superseded` or `conflict`. An error from the advance leaves the outcome unknown, and the record
  stays;
- at service startup, while it holds the request-socket lock and that System's advisory lock, a
  single record at head sequence + 1 that chains to the unchanged head, or the only record of a
  lane that has no head.

Before removal, the authority preserves the record's exact bytes in a reserved `retracted/`
subdirectory of the journal directory: mode 0700, owned by the authority, created on first use.
The lane inventory accepts exactly that name, and only as a real directory (not a symlink) with
that owner and mode. It never enumerates or reads the directory's contents as a lane, and every
other entry that is not `<uuid>.jsonl` still fails `unsafe-tree`. Every other difference still
refuses service: a longer suffix, a head that moved, a shorter journal, or any divergence. The
trusted head never moves, and no anchored record is removed.

Rejected for this amendment:

- **Advance the head before the local fsync.** judgment: a crash between the two then leaves a
  journal shorter than its head, which is the unrecoverable case this ADR refuses.
- **Tolerate a one-record tail at readiness.** verified: `FileAuthorityJournal._prepare_record`
  (`journal.py`, commit 83e79f112) requires `sequence == count + 1`, while the service numbers
  its next record from the head-length record list. The next append on the lane then fails as
  non-contiguous.
- **Only an operator repair command.** judgment: every refused anchor would still take the host
  out of service until an operator acted.
- **Evidence under `state_dir`.** verified: every authority systemd unit runs
  `ProtectSystem=strict`, and its `ReadWritePaths` name the journal directory and fixed
  subtrees but never `state_dir` itself
  (`deploy/systemd/system/kdive-external-boot-authority.service`,
  `deploy/ansible/roles/provider_authority_host/templates/authority.service.j2`,
  `deploy/ansible/roles/live_vm_host/templates/external-boot-authority.service.j2`). A new
  evidence path there fails with `EROFS`, and the host still restart-loops. Widening `ReadWritePaths` would require
  reprovisioning every host. The campaign orchestrator selected the reserved subdirectory on
  2026-09-28.

### Amendment (2026-09-28): the periodic readiness check waits out an in-flight anchor (#2899)

The anchor order (local fsync, then head advance) means a running authority always holds a
window where one lane's journal is one record ahead of its head, or a new lane has no head row.
The periodic readiness check observed that window and exited the host. When the periodic check
raises `head-mismatch` or `inventory-mismatch`, it re-reads the heads and reloads the lanes once
while the service holds new anchors and waits for in-flight ones to finish, including any
retraction of a refused record. A mismatch that persists refuses service as before. The wait and
the retry stay inside the readiness timeout. The startup and standalone checks are unchanged, and
the periodic check never retracts.

Rejected for this amendment:

- **Do nothing and let startup reconcile.** judgment: every overlap aborts the requests in
  flight, and nothing was wrong to reconcile.
- **Retry once after a short delay, without quiescence.** judgment: nothing makes the second
  read land outside an anchor, so it narrows the race without closing it.
- **Hold every lane lock during the check.** verified: `_release_lane` (`service.py`, commit
  b51e5c8c9) pops an idle lane, so a lane created during the check gets a fresh lock the check
  does not hold.
- **Treat a one-record tail as healthy at readiness.** judgment: the check could no longer tell
  a crash-stranded record from an anchor in progress, and the #2793 amendment already rejects a
  tolerated tail for the next append.
- **Serialize every anchor behind one service lock.** judgment: it trades a rare readiness retry
  for serialized head advances on all lanes during normal operation.

### Amendment (2026-09-29): the periodic retry also covers a torn or vanished lane (#2933)

The check's lane loads run beside an anchor with no reader exclusion, so an in-flight anchor has
two more views: a final line read before its newline, or a lane truncated mid-read, fails as
`journal: invalid-lane`; and a single-record lane that a refusal's retraction unlinks between
the lane listing and its stat fails as `journal: unsafe-tree`. The #2899 amendment deliberately
left both out of the retry set. The periodic check now also retries once under quiescence on
`invalid-lane`, and on an `unsafe-tree` whose only cause is that a listed entry no longer exists.
A wrong type, owner, mode, or name found by the lane listing is still refused at once; one found
only when the journal opens the lane reports `invalid-lane` and is refused after the one retry.
The reported component and reason are unchanged.

Widening is safe because the retry runs with every anchor drained: no lane is mid-append,
mid-advance, or mid-retraction, so a torn or missing lane seen then is at rest and refuses
service as before. The retry never tolerates, repairs, or retracts a lane. Startup and the
standalone check are unchanged, and a crash mid-append still leaves a torn lane that startup
refuses.

Rejected for this amendment:

- **Do nothing.** judgment: as in the #2899 amendment, every overlap aborts the requests in
  flight, and nothing was wrong at rest.
- **Skip a vanished entry in the lane listing.** judgment: the listing is shared with startup and
  the standalone check, whose reported reason for a vanished entry would then change; the
  operator chose the retry shape on 2026-09-29.
- **Quiesce anchors for every periodic pass.** judgment: it stalls anchors on every lane at every
  readiness interval to prevent a rare overlap the retry already absorbs.
- **Retry every `unsafe-tree`.** judgment: a structural cause cannot be produced by an anchor,
  and retrying it only delays the refusal of a foreign or wrongly owned entry.
- **Make the append atomic to readers.** judgment: it changes the journal write path the operator
  kept out of this scope, and still leaves the retraction race.

### Amendment (2026-09-29): startup removes a torn unanchored tail (#2983)

An append is an `os.write` loop and an `fsync` on an `O_APPEND` descriptor. A crash, power loss,
or write error part-way through leaves a final line with no newline; issuing a single `write` would
not change that, because a write is not guaranteed atomic across a crash or power loss. The anchor
order (local append, then head advance) means such a line was never anchored. This amendment
widens the #2793 startup rule to that line, and narrows the #2933 amendment's statement that a
lane torn mid-append is refused at startup to the cases listed below.

At startup only, under the same request-socket and advisory locks, a lane whose single defect is
a final line with no newline and no longer than one record is recovered when the complete records
before it end exactly at the head, or when the lane has no head and no complete record; a
zero-byte lane with no head (a first append that failed after creating it) is recovered too. The
authority preserves the torn bytes in `retracted/` under a name carrying their SHA-256, then
truncates the lane to the last complete record, or unlinks it; recovery needs room for that
evidence, so a lane torn by `ENOSPC` refuses until space is freed. A startup whose first check
fails with `journal: invalid-lane` now runs the reconcile step, and a lane it cannot recover still
refuses service. Still refused: a torn line after an unanchored complete record (at
most one tail per lane per startup), a head that is ahead of the complete records, an oversized
or non-final corrupt line. The periodic and standalone checks stay read-only, the append path and
record format are unchanged, and no anchored byte is removed.

Rejected for this amendment:

- **Make the append atomic** (write a temporary lane, rename it under a per-lane lock).
  verified: every append would rewrite the whole lane, bounded only by
  `DEFAULT_MAX_JOURNAL_BYTES` (64 MiB, `journal.py`). judgment: that cost and a lock the anchor
  and retraction paths would both need buy nothing startup recovery does not; the operator
  excluded it on 2026-09-29.
- **Length-prefixed or checksummed framing.** judgment: an on-disk format change with a
  migration for every existing lane, to detect a defect the newline already detects.
- **Truncate whenever the complete prefix validates, head or not.** judgment: a torn line after
  an unanchored complete record would then combine with the #2793 retraction and remove two tails
  in one startup, and a head ahead of the journal would pass as recoverable.
- **Do nothing; the operator repairs.** judgment: every interrupted append then takes the host out
  of service until someone edits the lane, although nothing anchored was lost.

### Amendment (2026-09-30): takeover recovery preserves the recorded mutation (#2977)

When takeover recovers a suspended operation, it rebuilds that operation from its last journal
record and anchors the remaining `provider-returned`, `observed`, and `terminal` records from the
rebuilt request. The rebuilt request carries every mutation field of the recorded operation
(binding, attempt, `local_timing`, identities, recovery objects), so each record recovery anchors
carries the mutation fields the original attempt would have written; its sequence, chain, and a
fresh observation still differ. Before
this amendment the rebuild dropped the ADR-0684 `local_timing` snapshot. The recovered `terminal`
then no longer matched a successor's release-phase request, and a retry that superseded a
completion still in flight failed with `journal_conflict` (`release_phase_mismatch`) instead of
adopting it. A refused `terminal` is still retracted and recovered as the #2793 amendment
describes; only the rebuilt fields change. The record format, the adoption check, and takeover
ordering are unchanged.

Rejected for this amendment:

- **Serialize takeover against an in-flight anchor for the same lane.** verified: the refused
  `terminal` is already recovered and anchored under the successor's watermark, so waiting for
  it adds nothing once the rebuilt record matches. The operator dropped this option on
  2026-09-30.
- **Leave `local_timing` out of the adoption match.** judgment: the match binds the timing
  snapshot so that a replay or adoption cannot run under different deadlines than the recorded
  attempt.

### Amendment (2026-10-01): a teardown generation outlives its Allocation (#2992)

Purpose `teardown` is allocated, acknowledged, and committed on an Allocation in any state. The
three functions that required an `active` Allocation for every purpose
(`allocate_external_boot_authority`, `acknowledge_external_boot_authority`, and
`commit_external_boot_authority_result`) now require it only for `activate`, `recover`,
`resolve-conflict`, and `release`. Migration 0169 makes the change. Every other part of the
binding is unchanged: the credential, the job attempt, the generation, the acknowledgement, the
System state, and the newest activation. Each function binds `p_purpose` to the marked or stored
purpose in the same predicate, so a generation of another purpose cannot pass as a teardown.

Lease expiry ends an Allocation without tearing down its Systems, and only the authority teardown
may finish a System with external-boot history (ADR-0620). Before, such a System had no exit.
Tearing it down needs no live Allocation, because it removes the System rather than using it.

Rejected for this amendment:

- **Do nothing.** verified: `test_0168_expired_allocation_still_supersedes` (removed by this
  change, at ccc8b4329) showed the allocator answering `superseded` for a teardown on an
  `expired` Allocation, which leaves the System with no supported exit.
- **Admit only `released` and `expired`.** judgment: no other non-`active` state needs a
  separate rule. A teardown is the operation that every Allocation end needs, so a list would
  only add a case to maintain.
- **Relax only the allocator.** verified: acknowledgement and commit each test
  `v_allocation.state <> 'active'` (`0122_external_boot_authority.sql` lines 651 and 905), so a
  teardown would allocate and then fail at acknowledgement.

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
- Accounted cleanup now has its finalizer. The provider adapter receives the `sequence` and digest
  of the `mutation-started` record this authority anchored for that same mutation, as the
  service-constructed `AuthorityCommitContextV1` that [ADR-0592](0592-authority-commit-context-carries-the-anchored-journal-proof.md)
  adds to the `commit` seam. The local adapter's `cleanup` commit point builds its finalization
  proof from that context, so the tombstone ADR-0586 retains is discharged only by a caller holding
  values it could not have forged, and this authority's journal record of that commit is the
  evidence the discharge happened. State the limit precisely: the local store still compares only
  the proof's `binding` and `point_digest`, and `CleanupTombstoneV1` persists no journal field, so
  the journal values are carried and authenticated at the seam rather than checked or retained by
  the store. The `teardown` commit point still publishes a tombstone with no finalizer; that
  remains open and belongs to the local adapter.

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
