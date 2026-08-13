# 0556 — Reclaim orphaned captures across providers exactly once

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

We will reclaim orphaned traffic-capture state from persisted job ownership, across every
provider that implements traffic capture, and persist completion so each row is reaped once.

The provider-agnostic sweep selects capture rows only after a settle window, resolves the
bound System and Resource through the Run, filters by Resource kind, and dispatches an
`OrphanedCapture` to that kind's `CaptureReaper`. It processes a bounded number of candidates
per pass so the historical backlog introduced at deployment drains over multiple passes.
There is no lookback cutoff. A persisted reap-once marker removes completed rows from later
passes, so an idle deployment performs no capture-reclamation work.

The shared port contract carries the provider kind, Resource identity, stored-or-derived
domain name, System id, and job id needed to name only the owning capture. The handler and
all reapers use one `capture_qom_id(job_id)` convention. A provider reaper must detach that
QOM object before removing the destination and must tolerate an already-missing filter,
domain, or destination. One row's failure is logged with `(system_id, job_id)` and does not
stop the rest of the pass; the failed row remains eligible for a later pass.

Remote-libvirt binds the reaper to the row's Resource using ADR-0187 and deletes the named
libvirt storage volume. It does not fan out through the fleet reaper bundle. Local-libvirt
detaches from the local domain and removes the pcap at the shared runtime-path convention.
The local implementation must first establish that the process performing reconciliation
can reach that worker-owned path; #1948 owns whether that is a colocated reconciler reaper or
a worker-side execution of the same port contract.

The sweep owns all three leak classes, including residue from a `succeeded` job. Best-effort
reclaim remains non-masking: cleanup failure must not turn a successful capture into a failed
job or hide its artifact. #1949 owns the smaller persistence detail that makes such failure
selectable—recording the failed reclaim or widening candidate selection—but it must preserve
both that non-masking rule and the reap-once convergence rule.

Candidate selection uses the database reference clock. The settle duration is an operator
configuration stated as a duration in seconds per terminal job row, measured from the job's
database-maintained `updated_at`. Before it expires the row is skipped; after it expires the
row can be reclaimed. A later pass is the recovery action for a failed attempt. The concrete
default is chosen and documented with #1946 because a lapsed lease means a dead or wedged
worker and provides no derived upper bound.

## Consequences

- Local-libvirt and remote-libvirt share one ownership and ordering contract while retaining
  provider-specific destination removal.
- Detach-before-remove prevents QEMU from continuing to write an unlinked destination.
- The marker adds a migration and one completion write per reclaimed capture row. This is
  intentional write traffic on the jobs table; it replaces both repeated no-op connections
  and permanent abandonment by a lookback.
- The first deployment exposes the full historical backlog. Per-pass bounding limits work,
  but draining may take several reconciler intervals and progress must remain observable in
  per-pass counts and per-row failure logs.
- A row whose Run was never bound, whose ownership chain was removed, or whose provider kind
  has no reaper cannot be reclaimed safely. It is skipped and logged; the sweep does not guess
  a host, domain, or path.
- The row-keyed design cannot discover a capture destination whose job row is absent. Jobs are
  retained by current schema policy, so this is accepted as a narrower blind spot than scanning
  every provider's storage namespace.
- #1948 must settle local path reachability before implementation. #1949 must settle how a
  failed best-effort reclaim becomes selectable. These choices may refine mechanics but may
  not weaken cross-provider ownership, detach-before-remove, succeeded-residue coverage, or
  reap-once convergence.

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
