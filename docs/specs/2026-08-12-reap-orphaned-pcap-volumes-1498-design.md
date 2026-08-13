# Reclaim orphaned traffic captures across providers — design (#1943)

- **Supersedes:** the remote-only #1498 design previously stored at this path
- **Architecture:** [ADR-0556](../adr/0556-reclaim-orphaned-captures-across-providers.md)
- **Epic:** [#1943](https://github.com/randomparity/kdive/issues/1943)
- **Implementation entries:** #1951 (operation supervision), #1952 (publication fencing),
  #1946 (spine), #1947 (remote), #1948 (local), #1949 (succeeded-row residue)

## Outcome

Every orphaned `capture_traffic` filter and destination has a durable owner outside the
leaking job. Reconciliation works for local-libvirt and remote-libvirt, detaches before it
removes, drains historical state in bounded passes, and records completion so an idle
deployment performs no repeated capture-reclamation work for eligible rows.

## Scope

This design owns the shared `capture_qom_id`, a provider-neutral `CaptureReaper` port, the
reap-once marker and candidate sweep, provider implementations for local-libvirt and
remote-libvirt, and the persistence needed to recover residue from best-effort reclaim. A
future provider does not inherit reaper support merely by advertising traffic capture.

It does not change capture duration, polling, fetch, trim, artifact contents, job retry or
dead-letter policy, MCP tools, or agent-facing schemas. #1952 changes publication ordering but
not the artifact an agent receives. It does not add capture reapers for provider families that
do not implement traffic capture. The independent `capture_vmcore` guard defect is owned by
#1945 and ADR-0094, not this decision.

## Invariants

1. `capture_qom_id(job_id)` has one definition used by the handler and every reaper.
2. A reaper detaches the job's filter before removing its destination.
3. Candidate ownership resolves from `jobs.payload.run_id` through the bound Run, System,
   Allocation, and Resource; the sweep never reads a nonexistent payload `system_id`.
4. Resource-kind filtering prevents one provider's row from reaching another provider's
   reaper.
5. A persisted marker removes a resolved row from future passes. There is no lookback;
   provider calls are at-least-once and idempotent across a crash before the marker write.
6. A bounded batch size paces the initial historical backlog.
7. One candidate failure does not starve the pass and remains retryable after a persisted,
   database-clock backoff deadline.
8. Cleanup residue from a `succeeded` job remains owned without making best-effort reclaim
   mask a successful capture or its artifact.
9. Every capture attempt clears prior completion before it can recreate provider state.
10. A per-job advisory ownership fence serializes provider-state creation and reaping.
11. Loss of the lock-owning session terminates provider work, and reaping waits for positive
    cancellation completion for that attempt.
12. Cancellation completion also proves that object-store and metadata publication can no
    longer commit for that attempt.

## Components

### Shared convention and port (#1946)

`providers/ports/traffic.py` owns `capture_qom_id(job_id)`. The handler imports it instead
of formatting the QOM id inline.

`providers/infra/reaping.py` adds a frozen `OrphanedCapture` value, a `CaptureReaper`
protocol, and `NullCaptureReaper`. The value carries Resource identity and kind,
`domain_name`, `system_id`, and `job_id`; it does not carry a client-supplied destination.
Provider implementations derive destinations through their existing naming functions.

### Marker and sweep (#1946)

A migration adds the capture reap-once state to durable job ownership. The sweep and the
marker's first write path ship together so the new field is neither dead schema nor an
unverified mechanism.

The sweep joins:

```text
jobs j
  → runs rn        on rn.id = j.payload.run_id
  → systems s      on s.id = rn.system_id
  → allocations a  on a.id = s.allocation_id
  → resources res  on res.id = a.resource_id
```

It selects eligible, unmarked rows older than the configured settle duration using the
database clock, orders them deterministically, and limits each pass. Eligibility requires a
resolvable ownership chain and a registered reaper for the Resource kind, so an invalid row
cannot consume the batch or starve reclaimable work. A successful provider call and marker
write form one logical candidate outcome: if the provider succeeds but the marker write does
not, the next pass repeats an idempotent reclaim. If provider reclaim fails, persisted state
records a bounded backoff deadline rather than completion. Selection ignores retries whose
deadline has not arrived and orders by an explicit untouched-row discriminator, then retry
deadline and job update time. A failure advances its deadline beyond its prior value and the
current database time, so untouched rows sort ahead even when backoff expires before the next
pass.

The capture handler holds a session-level per-job advisory ownership fence from before it
clears prior completion until provider detach and destination reclaim finish. The sweep takes
the same fence before a provider call and holds it through the completion write. An unavailable
fence defers the row. Process death releases the fence; a live delayed worker cannot create
state after an absence-tolerant reap.

### Operation supervision prerequisite (#1951)

#1951 introduces one durable authoritative operation identity per queue attempt. It must make
provider mutation impossible until exact operation identity is recoverable, terminate the current
operation when its lock-owning session is lost, survive worker and supervisor replacement, and
produce provider-specific positive quiescence evidence. It also owns the coordinated legacy-worker
rollout fence. The detailed launch state machine and quiescence probes belong to #1951's governed
design; this record fixes their externally observable safety contract rather than preselecting a
launcher protocol.

### Publication-fencing prerequisite (#1952)

#1952 makes object upload and metadata registration phases of the authoritative supervised
attempt. Publication may commit only while the attempt remains current and fenced. Cancellation
must prevent a later commit or remove an unregistered object before it acknowledges completion.
The detailed commit and rollback ordering belongs to #1952's governed design and depends on the
attempt identity supplied by #1951.

Lock availability is necessary but insufficient for reaping. #1946 requires positive evidence
that the authoritative attempt is quiescent and that its publication phase can no longer commit
before it calls a provider or writes completion. Evidence from a superseded attempt, a missing
link, process disappearance alone, or an asynchronous cancellation request is insufficient.
Failure to establish either prerequisite defers the row and emits an owner-keyed failure. The
capture sweep remains disabled until #1951's positive legacy rollout fence and #1952's publication
contract are deployed. Restoring provider reachability or completing the rollout is the recovery
action; eventual reclamation is conditional on them.

The sweep never invents missing ownership or provider wiring. Each provider failure is logged
with `(system_id, job_id)`. A pass reports attempted, reclaimed, skipped, and failed counts
through the reconciler's existing observability path. Idle means no eligible unmarked row
remains; lifecycle integrity checks retain ownership of malformed durable chains.

The settle limit's full contract is:

- unit: seconds;
- reference clock: PostgreSQL `now()` against database-maintained `jobs.updated_at`;
- scope: per terminal candidate row;
- consequence before expiry: the row is not dispatched;
- recovery after a failed dispatch: it remains unmarked and a later reconciler pass retries.

#1946 chooses and documents the concrete default with its configuration field because no
heartbeat-derived upper bound exists for a dead or wedged worker. The duration is pacing, not
the safety fence: the per-job advisory ownership fence prevents reclamation while a live worker
still owns provider state. A wedged worker with a live database session delays reclamation;
session loss triggers termination, and the reaper waits for its positive acknowledgment.

### Remote-libvirt implementation (#1947)

The reaper binds the row's Resource with `remote_config_for_resource` under ADR-0187. Over
one connection it tolerantly deletes `capture_qom_id(job_id)` from the stored-or-derived
domain, then tolerantly deletes `pcap_volume_name(system_id, job_id)` from the configured
pool. Errors other than not-found remain categorized failures. It never uses the fleet
reaper bundle.

### Local-libvirt implementation (#1948)

The reaper tolerantly detaches the same QOM object, then removes
`pcap_path(system_id, job_id)`. #1948 must first record whether the reconciler is colocated
with the worker-owned runtime path. If not, the work executes worker-side behind the same
ownership and port contract. It also defines retry behavior when a stale pcap already exists
at that path. Those choices may not weaken detach-before-remove or reap-once convergence.

### Succeeded-row residue (#1949)

Both providers keep reclaim non-masking. The migration treats pre-existing `succeeded` rows as
unknown and drains them once because no historical cleanup result exists. Captures completed
after migration record immediate cleanup success so the sweep selects only failed best-effort
reclaim. #1949 chooses the marker representation and provider-to-job write path for those
semantics. It must prove:

- injected reclaim failure leaves the job successful and its artifact visible;
- both providers later reclaim the residue;
- a post-migration capture whose reclaim succeeded is not revisited unless that job retries;
- every retry clears prior completion before attach or destination creation, so a later worker
  death cannot hide new provider state behind an earlier attempt's marker.

## Failure and recovery

- **Worker dies with a filter attached:** abandonment makes the row terminal; after settle,
  the reaper detaches and removes the destination.
- **Job retries after earlier cleanup:** the new attempt clears completion before creating
  provider state and becomes the job's current attempt while holding the ownership fence; a
  later failure remains eligible and the prior acknowledgment cannot satisfy it.
- **Operation supervisor or launcher fails:** #1951's durable state machine recovers the exact
  attempt and either proves it never became mutable or terminates it before acknowledging
  quiescence.
- **Terminal worker is still alive:** its ownership fence defers reaping; it cannot create
  provider state after an absence-tolerant completion write.
- **Lock-owning database session is lost:** the supervisor terminates provider work; the sweep
  defers until cancellation completion for that attempt is recorded.
- **Publication is in flight when ownership is lost:** #1952 prevents its commit or removes the
  unregistered object before cancellation completion; the sweep remains deferred until then.
- **Supervisor dies after termination but before acknowledgment:** #1951 requires its replacement
  to recover the attempt and reproduce the provider-specific positive quiescence evidence before
  recording completion.
- **Cancellation cannot be confirmed:** no provider reap or completion write occurs; the row
  remains observable and retryable rather than risking post-reap publication.
- **Provider unavailable:** the row remains unresolved, the failure is logged with its owner
  ids, and a later pass retries after persisted bounded backoff without starving later rows.
  A permanently unreachable provider is an explicit fail-closed exception to eventual reclaim.
- **Filter, domain, or destination already absent:** provider reclaim treats absence as
  success and the row is marked.
- **Provider succeeds but marker write fails:** the next pass repeats the idempotent provider
  reclaim, then retries the marker.
- **Ownership chain missing or provider unregistered:** the row is ineligible and cannot
  consume the batch; lifecycle integrity checks own diagnosis of the broken chain.
- **Historical backlog:** the batch limit drains it across passes without a lookback cutoff.
- **Legacy rollout not quiescent:** the affected provider-kind sweep remains disabled; operators
  finish stopping old workers or restore remote reachability, then retry the cutover.

## Verification

The implementation entries prove the design at their natural boundaries:

- real payload fixtures prove Run-addressed selection;
- database tests prove settle, marker convergence, provider-kind filtering, deterministic
  bounded draining, missing ownership, per-row isolation, and the cancellation-complete gate;
- recording fakes prove detach-before-remove for both providers;
- provider tests prove tolerant absence, binding, destination naming, and surfaced errors;
- fault tests drop the lock-owning connection at each provider lifecycle boundary and prove
  termination precedes any reaper call or completion write;
- #1951 fault and recovery tests prove every launch state, session-loss cancellation,
  provider-specific quiescence, supervisor replacement, and the positive legacy rollout fence;
- #1952 fault tests prove every upload, metadata, cancellation, cleanup, and acknowledgment
  boundary leaves neither a late artifact nor an unregistered object;
- injected reclaim failure proves L3 recovery stays non-masking;
- `just ci` is the repository-wide gate after every entry.

No live test claims to reproduce a worker death between attach and detach deterministically.
Remote mechanics ride the existing `live_vm_remote` tier where available; local live proof
uses the existing native `live_vm` tier when #1948's reachability decision permits it.

## Acceptance criteria

1. A pcap left by a retry-exhausted, canceled, succeeded-with-reclaim-failure, or
   worker-killed `capture_traffic` job is eventually reclaimed on both providers once its owning
   provider is reachable.
2. Any still-attached filter is detached before its destination is removed.
3. A concurrent live capture on the same System is never targeted.
4. A resolved row is absent from the next pass; a crash before its marker may repeat the
   idempotent call, and an idle deployment with no eligible unmarked rows does zero work.
5. A backlog larger than the batch bound drains over multiple passes.
6. One provider or row failure does not starve other candidates.
7. A successful capture remains successful when its immediate reclaim fails, and later
   recovery does not hide its artifact.
8. Every child entry passes `just ci` and cites ADR-0556 rather than ADR-0555.
