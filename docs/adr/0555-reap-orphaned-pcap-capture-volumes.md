# 0555 — Reclaim orphaned captures from the job row, detaching the filter before the volume

## Status

Accepted (2026-08-12)

## Context

`RemoteLibvirtTrafficCapture` (ADR-0432, #1434) writes each capture to a storage volume on the
remote host, named deterministically from the owning System and job:

```
kdive-pcap-<system_id>-<job_id>.pcap
```

Three paths reclaim it today, and all three run inside the owning job:

- the handler's `finally` reclaim around store (`capture_traffic.py:392`);
- the cancel-path reclaim when the poll loop observes a canceled job (`capture_traffic.py:352`);
- `prepare`'s pre-delete of *this job's own* stale volume, so an at-least-once retry starts clean.

A job that exhausts its bounded retries never runs again — `running → queued` is the retry edge,
and exhaustion lands the row in `failed`. So none of the three fires, and the pcap stays in the
operator's pool. That is #1498.

**The capture is not just a file.** The handler attaches a QEMU `filter-dump` object
`kdive-dump-<job_id>` (`capture_traffic.py:333`) and removes it only in the `finally` at `:350`.
A worker killed between those two points leaves the filter bound to a *live* domain, and nothing
else ever removes it: `attach`'s tolerant `object-del` is keyed to the same `qom_id` so only a
retry of that job clears it, a different capture uses a different `qom_id`, and
`repair_leaked_domains` only destroys domains whose System row is gone — which this System's is
not.

That rules out reaping by listing the pool and aging on volume mtime. An attached filter keeps
appending, so the mtime never goes stale and the volume is never selected; and if the guest goes
idle long enough to be selected, deleting the volume unlinks an inode QEMU still holds open —
space is not reclaimed until the domain stops, the filter keeps writing into the unlinked inode,
and `listAllVolumes` can no longer see it. A bounded visible leak becomes an unbounded invisible
one.

The owning job row is a better handle than the pool listing. `jobs` rows are never deleted
(`db/schema/0038_runs_failing_job_id.sql` records that as a relied-upon invariant), the row id
*is* the `job_id` in both the volume name and the QOM id, and `payload->>'system_id'` gives the
System — so from a terminal row both names are reconstructible exactly, with no listing, no
name parsing, and no mtime.

## Decision

We will reclaim orphaned captures from the job row, detaching the filter before deleting the
volume.

**Sweep** (`reconciler/cleanup/provider_reaping.py::reap_orphaned_captures`). Select `jobs`
rows where `kind = 'capture_traffic'`, `state IN ('failed', 'canceled')`, and
`now() - lookback < updated_at < now() - settle`. For each, call the provider port once with
`(system_id, job_id)`.

- **`succeeded` is excluded.** The handler's `finally` runs before it returns, and the worker
  marks the row succeeded only after that — so a succeeded row's volume is already reclaimed and
  sweeping it is pure waste.
- **`settle`** (default 15 minutes) is the only timing guard: how long after a row goes terminal
  we wait before assuming the job's own cleanup will not run. It matters because
  `repair_abandoned_jobs` dead-letters a lease-lapsed, attempts-exhausted `running` row to
  `failed` while that worker may still be alive.
- **`lookback`** (default 24 hours) bounds the scan. Rows are never deleted, so without it every
  historical capture row is re-swept forever.
- `updated_at` is trigger-maintained (`jobs_set_updated_at`), so no migration is needed.

**Port** (`providers/infra/reaping.py`) — `CaptureReaper.reclaim_capture(system_id, job_id)`.
One method, because the two libvirt calls must happen in one order on one connection and the
naming conventions belong to the provider, not the reconciler.

**Implementation** (`providers/remote_libvirt/reaping/capture.py`) — reconstruct
`capture_qom_id(job_id)` and `pcap_volume_name(system_id, job_id)`, then over one `qemu+tls://`
connection: `object-del` the filter on `domain_name_for(system_id)`, then delete the volume.
Both tolerate not-found; a missing domain is not an error. Ordering is load-bearing — deleting
first is the unlinked-inode failure above.

**Shared convention** — `capture_qom_id(job_id)` moves to `providers/ports/traffic.py`, which
already documents the `qom_id` contract, and the handler imports it instead of minting the
f-string inline. Both sides must agree on the string, and a duplicated literal is exactly the
drift this avoids. That is the only change to the capture path.

## Consequences

- A pcap left by a retry-exhausted, canceled, or worker-killed `capture_traffic` job is
  reclaimed, and the orphaned filter is removed with it. For a worker-killed job this follows
  `repair_abandoned_jobs` dead-lettering the lease-lapsed, attempts-exhausted `running` row to
  `failed`.
- A concurrent capture on the same System is untouched: the sweep addresses one exact
  `(system_id, job_id)` pair drawn from a row that is already terminal, so a live capture — a
  different `job_id`, a `running` row — is never selected. No age heuristic stands between the
  sweep and a live capture, because the sweep cannot name one.
- Cost per pass is one unindexed `jobs` scan plus two libvirt calls per selected row, against
  the pool listing plus a second per-host connect and `pool.refresh(0)` the rejected design
  needed. No index is added: `repair_abandoned_jobs` already runs an unindexed per-pass `jobs`
  scan, and indexing one sweep and not its sibling would be arbitrary. If that scan becomes a
  problem it is a tuning change for both.
- **Residual: `settle` is a heuristic, not a derived bound.** The lease is heartbeat-renewed
  (`worker.py:155` rejects `heartbeat_interval > lease/3`), so a lapsed lease means the
  heartbeat *stopped* — a dead or wedged worker, off the nominal timeline by construction.
  Nothing bounds how long such a worker might still hold the volume, so no value is provably
  safe. 15 minutes is chosen as comfortably past a healthy capture
  (`CAPTURE_MAX_DURATION_S` is 300s) plus its fetch/store tail. The residual is accepted
  because the capture being clobbered has already dead-lettered — its artifact is lost either
  way.
- A volume whose row is outside the lookback window is not reclaimed. Rows are never deleted,
  so widening the window is the remedy if that is ever observed.
- Nothing reads the `kdive-pcap-` prefix any more, so a stray file matching it is left alone
  rather than deleted on a name match.

## Considered & rejected

- **List the storage pool and age on volume mtime** (the shape #1498 proposes, mirroring
  ADR-0094). Rejected on the evidence in Context: an attached filter keeps the mtime fresh so
  the leak is never selected, and selecting it after an idle window unlinks an inode QEMU still
  writes to. It also needs a name parser, an unparseable-name branch, and a per-host connect and
  pool refresh the row-keyed sweep does not.
- **Extend `DumpVolumeReaper` to return both kinds.** Makes the port's name a lie, forces
  `reap_orphaned_dump_volumes` to branch across two different guards, and changes an ADR-0094
  contract for a reason unrelated to host_dump.
- **Do nothing; rely on `prepare`'s per-job pre-delete.** It only runs when that same `job_id`
  runs again, which a retry-exhausted job never does. This is the leak.
- **Reclaim at dead-letter time inside the job queue.** Couples the queue to provider storage
  and needs a working `qemu+tls://` connection at the moment of failure — often exactly when the
  host is unreachable. It also misses a worker that died without recording the transition, which
  is the case that leaves the filter attached.
- **Delete the volume without detaching the filter.** Simpler by one libvirt call, and wrong:
  QEMU holds the fd, so no space is reclaimed until the domain stops and the filter keeps
  writing into an unlinked inode that no later pass can see.
- **Add a `reaped` marker column to `jobs` instead of a lookback window.** A migration and new
  write traffic on the queue's hot table, to save a bounded scan the sibling sweep already pays
  unindexed. The lookback is weaker — a row older than the window is never revisited — and that
  is the trade accepted.
- **Sweep `succeeded` rows too, for symmetry.** Their `finally` provably ran before the row was
  marked, so it is cost with no case behind it.
