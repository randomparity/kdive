# 0556 — Reclaim orphaned captures across providers to convergence

## Status

Accepted (2026-08-12)

## Context

ADR-0555 chose a remote-libvirt-only sweep of terminal `capture_traffic` job rows. It
detached the job's QEMU `filter-dump` object before deleting the pcap volume, bounded
repeated work with a lookback window, and excluded `succeeded` jobs because it assumed their
destinations had already been reclaimed.

Further review for #1943 disproved the scope and two of those bounds:

- Local-libvirt wires the same `TrafficCapturer` handler and advertises traffic-capture
  support. A worker death can therefore orphan the same filter and its worker-local pcap.
- A lookback bounds scans by permanently abandoning older rows. At deployment, every
  existing orphan can already be older than that bound. Rows inside the window are also
  revisited on every reconciler pass because the sweep records no completion.
- Reclaim is deliberately best-effort on both providers. A transient reclaim failure can
  leave a destination behind while the job still records `succeeded`, so success is not
  evidence that host state is gone.

The leak has three forms: an attached filter left by a dead worker, a destination owned by
a `failed` or `canceled` row, and a destination left after best-effort reclaim on a
`succeeded` row. The first form makes storage-pool mtime insufficient: an attached filter
keeps writing, and deleting its destination first turns a visible volume into an unlinked
inode that remains open until the domain stops.

The job row remains the durable correlation key. `capture_traffic` is Run-addressed, so the
sweep must resolve the System and Resource through
`jobs → runs → systems → allocations → resources`; the payload has no
`system_id`. A bound Run retains its System through its lifecycle even though the schema
allows an unbound Run, and missing downstream ownership rows make a candidate unreclaimable
rather than safe to guess.

## Decision

We will reclaim orphaned traffic-capture state from persisted job ownership for the two
providers that currently implement it, local-libvirt and remote-libvirt, and persist completion
so each resolved row leaves the candidate set after one successful attempt. A future provider's
capture-reclamation contract requires its own decision rather than inheriting support from a
capability flag alone.

The provider-agnostic sweep selects capture rows only after a settle window, resolves the
bound System and Resource through the Run, filters by Resource kind, and dispatches an
`OrphanedCapture` to that kind's `CaptureReaper`. It processes a bounded number of candidates
per pass so the historical backlog introduced at deployment drains over multiple passes.
There is no lookback cutoff. A persisted reap-once marker removes completed rows from later
passes. Provider cleanup and the database write cannot share a transaction: a crash after
provider success but before the marker write repeats an already-effective call. Reapers are
therefore idempotent and tolerate absent state. The guarantee is at-least-once attempts with a
convergent effect, not exactly-once execution.

The shared port contract carries the provider kind, Resource identity, stored-or-derived
domain name, System id, and job id needed to name only the owning capture. The handler and
all reapers use one `capture_qom_id(job_id)` convention. A provider reaper must detach that
QOM object before removing the destination and must tolerate an already-missing filter,
domain, or destination. One row's failure is logged with `(system_id, job_id)` and does not
stop the rest of the pass. The persisted reap state carries a database-clock retry deadline;
failure advances that deadline beyond both its prior value and the current database time with
bounded backoff. Selection orders first by an explicit untouched-row discriminator, then by
retry deadline and job update time; it does not depend on a database's NULL ordering. Untouched
rows therefore sort ahead of a just-failed row even if its backoff expires before the next pass,
so persistent old failures cannot starve later candidates.

Provider-state creation and reaping share a per-job Postgres advisory ownership fence. A worker
holds that session-level fence from before it clears prior completion until after detach and
destination reclaim. The reaper acquires the same fence before it inspects or removes state and
holds it through the completion write. Process death releases the worker's fence; a paused or
partitioned live worker retains it and cannot race the reaper. If the fence is unavailable, the
row is deferred without consuming a provider call. This positive ownership boundary, rather
than the settle duration, prevents state from being created after an absence-tolerant reap.

The lock-owning connection is also the provider operation's cancellation authority. Loss of that
session must initiate termination of the current durable capture attempt. Lock release alone is
not evidence that provider mutation stopped: the reaper remains closed until durable evidence
proves the authoritative attempt can no longer mutate provider state. Older or superseded
attempts cannot satisfy that gate. #1951 owns the operation state machine, recoverable launch and
supervision protocol, provider-specific quiescence evidence, cancellation bounds, and positive
legacy-worker rollout fence needed to make this contract falsifiable.

Artifact publication is part of the same ownership boundary. Object-store upload and database
metadata registration must not commit after the authoritative attempt loses its fence, and a
cancellation acknowledgment is incomplete while either can still commit. Cancellation removes
any unregistered object before acknowledgment; a registered artifact remains governed by its
durable metadata. #1952 owns the publication state machine, commit barrier, rollback ordering,
and fault proofs. It depends on #1951 because publication must name the supervised attempt whose
termination evidence it extends.

#1946 may ship the sweep after #1951 and #1952 land, but leaves each provider kind disabled until
its concrete #1947 or #1948 reaper is registered. `NullCaptureReaper` is disabled wiring: it is
never eligible for dispatch and cannot produce a completion marker. For a post-cutover row, the
sweep requires positive quiescence and publication-closure evidence for the job's authoritative
attempt before the first provider call or completion write.

Pre-cutover rows use an explicit alternative evidence path because they have no supervised
attempt. The rollout records a durable cutover generation per provider kind and aggregate
operation-quiescent and publication-closed acknowledgments. The aggregate becomes complete only
after every legacy worker host for that kind is positively drained and prevented from rejoining
and, for remote libvirt, every affected Resource completes its transport observation. Completion
then samples the database-clock cutoff and commits the cutoff with the complete generation in one
transaction. A job admitted during the drain is therefore covered if a legacy worker could have
claimed it; a supervised attempt remains governed by its stronger attempt-linked evidence. A row
is covered only when its Resource kind matches and its database `created_at` is no later than that
cutoff. A missing attempt link is accepted only for such a covered row; it remains fail-closed
after the cutoff.
#1946 evaluates this predicate before dispatch. When either evidence path cannot be established,
the row remains deferred and observable. Eventual convergence is therefore conditional on the
owning provider becoming reachable and the prerequisite protocols completing.

Remote-libvirt binds the reaper to the row's Resource using ADR-0187 and deletes the named
libvirt storage volume. It does not fan out through the fleet reaper bundle. Local-libvirt
detaches from the local domain and removes the pcap at the shared runtime-path convention.
The local implementation must first establish that the process performing reconciliation
can reach that worker-owned path; #1948 owns whether that is a colocated reconciler reaper or
a worker-side execution of the same port contract.

The sweep owns all three leak classes, including residue from a `succeeded` job. Existing
`succeeded` capture rows predate the marker, so they enter the paced historical drain once;
there is no durable evidence that distinguishes an already-removed destination from residue.
For captures completed after the migration, the in-job cleanup path records successful cleanup
and leaves only a failed best-effort reclaim eligible. Cleanup failure must not turn a
successful capture into a failed job or hide its artifact. #1949 owns the schema and write-path
mechanics for that outcome, but not whether historical success is covered or whether future
successful cleanup is revisited. Every capture attempt clears prior completion before it can
attach the filter or create the destination, while holding the ownership fence. A crash after
clearing but before creation yields an eligible idempotent no-op; clearing after creation would
leave a retry-created orphan hidden behind the previous attempt's marker.

Candidate selection uses the database reference clock. The settle duration is an operator
configuration stated as a duration in seconds per terminal job row, measured from the job's
database-maintained `updated_at`. Before it expires the row is skipped; after it expires the
row can be reclaimed. A later pass is the recovery action for a failed attempt. The concrete
default is chosen and documented with #1946 because a lapsed lease means a dead or wedged
worker and provides no derived upper bound. Settle is therefore a pacing heuristic, not a
safety fence. A terminal job's worker may still be alive after the duration, but its ownership
fence prevents reclamation from pre-empting its late write or fetch. A wedged worker that keeps
its database session open also keeps its host state. Session loss cancels its provider operation;
reaping waits for positive termination acknowledgment rather than treating lock release alone as
proof that the worker stopped.

## Consequences

- Local-libvirt and remote-libvirt share one ownership and ordering contract while retaining
  provider-specific destination removal.
- Detach-before-remove prevents QEMU from continuing to write an unlinked destination.
- The marker adds a migration and one completion write per resolved capture row. This is
  intentional write traffic on the jobs table; it replaces both repeated no-op connections
  and permanent abandonment by a lookback.
- The first deployment exposes the full historical backlog, including successful captures
  whose destination is probably already absent. Per-pass bounding limits work, but draining
  may take several reconciler intervals and progress must remain observable in per-pass counts
  and per-row failure logs.
- Retry deadlines add one database-clock scheduling write after a failed attempt. Backoff keeps
  a degraded provider from monopolizing every pass; it also means recovery waits until that
  row's deadline, when the reconciler automatically makes it eligible again.
- Each active capture holds one database session for its ownership fence across the provider
  operation. This consumes one pool connection per concurrent capture. Database-session loss
  cancels the terminable provider operation and delays reaping until termination is acknowledged;
  #1951 must bound and test both resource use and cancellation latency.
- A provider operation that cannot be terminated safely cannot implement this capture lifecycle.
  Failing closed may retain host state longer, but it cannot publish new state after a reaper has
  marked the attempt complete.
- The durable operation supervisor becomes part of the worker deployment and recovery contract.
  It retains attempt identity and exit evidence across handler failure, and provider adapters own
  the stated quiescence probes. A supervisor replacement repeats observation rather than trusting
  a missing acknowledgment as evidence of continued work.
- Eventual reclamation requires the owning provider to become reachable. A permanently lost
  remote host leaves its rows deferred permanently; this is the fail-closed residual of choosing
  cancellation over post-reap publication risk.
- Artifact publication becomes a fenced attempt phase. #1952 must prove that cancellation either
  prevents its commit or removes an unregistered object before acknowledging termination.
- First deployment is a coordinated worker cutover, not an online schema-only rollout. Historical
  draining begins only after old capture producers are positively absent and remote transports are
  quiescent. This delays cleanup but prevents a legacy worker from publishing after a synthetic
  acknowledgment.
- A row whose Run was never bound, whose ownership chain was removed, or whose provider kind
  has no registered reaper is not an eligible candidate. Selection does not guess a host,
  domain, or path, and an ineligible row cannot consume the batch or starve eligible work.
  The idle-convergence claim applies when no eligible unmarked row remains; ownership-integrity
  diagnosis stays with existing lifecycle repair rather than becoming a second capture sweep.
- The row-keyed design cannot discover a capture destination whose job row is absent. Jobs are
  retained by current schema policy, so this is accepted as a narrower blind spot than scanning
  every provider's storage namespace.
- #1951 must establish operation quiescence and the rollout fence, and #1952 must establish
  publication closure, before #1946 ships the disabled sweep. #1947 and #1948 each enable only
  their concrete provider kind; null wiring never marks completion. #1948 must also settle local
  path reachability before implementation. #1949 must settle the
  marker and write-path mechanics for immediate cleanup outcomes. These choices may refine
  mechanics but may not weaken cross-provider ownership, detach-before-remove,
  succeeded-residue coverage, or reap-once convergence.

## Considered & rejected

- **List each storage pool and age volumes by mtime.** An attached filter keeps the mtime
  fresh, so the primary worker-death leak is never selected. Deleting after an idle period can
  instead unlink a file QEMU still has open. Local-libvirt's destination is not a storage-pool
  volume, so this also fails the cross-provider requirement.
- **Extend `DumpVolumeReaper`.** Dump volumes and captures have different ownership keys,
  liveness guards, and cleanup ordering. Combining them makes the existing port's name false
  and couples ADR-0094 host-dump behavior to traffic capture.
- **Reclaim at dead-letter time in the queue.** The queue would gain provider knowledge and
  need the failed provider to be reachable at the moment a job is declared dead. It also does
  not cover a worker that dies before recording its own terminal transition; the reconciler is
  the durable owner of that recovery.
- **Use a fleet-wide reaper instead of ADR-0187 Resource binding.** The job chain already names
  the Resource. Fanning out per row would cost one connection per configured host and could
  target a host that never owned the capture.
- **Keep ADR-0555's lookback instead of a reap-once marker.** A lookback permanently abandons
  old rows, including pre-deployment orphans, while repeatedly visiting every candidate still
  inside the window. It is a second, weaker convergence mechanism rather than a substitute for
  persisted completion.
- **Exclude `succeeded` rows.** Both provider reclaim paths are best-effort, so job success does
  not prove destination removal. Exclusion leaves leak class L3 without an owner.
- **Do nothing and rely on in-job cleanup or a retry.** Every leak in scope begins where the
  owning job did not complete cleanup, and a terminal job does not retry. No existing owner
  converges that state.
