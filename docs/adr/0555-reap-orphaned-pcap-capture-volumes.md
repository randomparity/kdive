# 0555 — Reclaim orphaned captures from the job row, detaching the filter before the volume

## Status

Accepted (2026-08-12)

> **Superseded by [0556](0556-reclaim-orphaned-captures-across-providers.md)** (2026-08-12)

## Context

`RemoteLibvirtTrafficCapture` (ADR-0432, #1434) writes each capture to a storage volume on the
remote host, named from the owning System and job — `kdive-pcap-<system_id>-<job_id>.pcap`.
Three paths reclaim it, and all three run inside the owning job: the handler's `finally` around
store (`capture_traffic.py:392`), the cancel-path reclaim (`:352`), and `prepare`'s pre-delete of
this job's own stale volume. A job that exhausts its bounded retries never runs again —
`running → queued` is the retry edge and exhaustion lands the row in `failed` — so none of the
three fires. That is #1498.

**The capture is not just a file.** The handler attaches a QEMU `filter-dump` object
`kdive-dump-<job_id>` (`capture_traffic.py:333`) and removes it only in the `finally` at `:350`.
A worker killed between them leaves the filter bound to a *live* domain, and nothing else removes
it: `attach`'s tolerant `object-del` is keyed to the same `qom_id` so only a retry of that job
clears it, a different capture uses a different `qom_id`, and `repair_leaked_domains` only
destroys domains whose System row is gone.

That rules out sweeping by listing the pool and aging on volume mtime. An attached filter keeps
appending, so the mtime never goes stale and the leak is never selected; and if the guest idles
long enough to be selected, deleting the volume unlinks an inode QEMU still holds — space
unreclaimed until the domain stops, the filter still writing, and the volume no longer listable.
A bounded visible leak becomes an unbounded invisible one.

The owning job row is the better handle. `jobs` rows are never deleted
(`db/schema/0038_runs_failing_job_id.sql` records that as a relied-upon invariant) and the row id
*is* the `job_id` in both the volume name and the QOM id.

## Decision

We will reclaim orphaned captures from the job row, detaching the filter before deleting the
volume.

**Selection** (`reconciler/cleanup/provider_reaping.py::reap_orphaned_captures`). `capture_traffic`
is **Run-addressed**: `CaptureTrafficPayload` carries `run_id`, `duration_s`, `max_bytes`,
`snaplen`, `capture_filter` under `extra="forbid"` — there is no `system_id` in the payload, and
the handler resolves the System through the Run. The sweep does the same, joining
`jobs → runs → systems → allocations → resources`:

```sql
SELECT j.id, s.id, COALESCE(s.domain_name, 'kdive-' || s.id), res.name
FROM jobs j
JOIN runs rn      ON rn.id = (j.payload->>'run_id')::uuid
JOIN systems s    ON s.id  = rn.system_id
JOIN allocations a ON a.id = s.allocation_id
JOIN resources res ON res.id = a.resource_id
WHERE j.kind = 'capture_traffic'
  AND j.state = ANY(ARRAY['failed','canceled'])
  AND j.updated_at < now() - :settle
  AND j.updated_at > now() - :lookback
ORDER BY j.updated_at
```

Every join is durable: `runs.system_id`, `systems.allocation_id` and `allocations.resource_id`
are all `NOT NULL` foreign keys, and none of those rows are deleted. `res.name` is nullable
(added in `0030_systems_inventory.sql`), so a row with no resource name is logged at warning and
skipped — for a remote-libvirt System that is a wiring fault, not an expected shape.

- **`failed` and `canceled`, not `succeeded`.** The handler's `finally` runs before it returns and
  the worker marks the row succeeded only afterwards, so a succeeded row's volume is already
  reclaimed. `canceled` is swept for the case the in-job reclaim at `:352` cannot cover: a cancel
  recorded while the worker was already dead, so the poll loop never observed it.
- **`settle`** (default 15 minutes): how long after a row goes terminal we wait before assuming
  the job's own cleanup will not run. It matters because `repair_abandoned_jobs` dead-letters a
  lease-lapsed, attempts-exhausted `running` row to `failed` while that worker may still be alive.
- **`lookback`** (default 24 hours) bounds the scan; rows are never deleted, so without it every
  historical capture row is re-swept forever.
- **`ORDER BY updated_at`** makes retries fair, so a persistently failing row cannot head-of-line
  block the rest across passes.
- `updated_at` is trigger-maintained (`jobs_set_updated_at`), so no migration is needed.

**Host binding.** Under ADR-0187 a per-op remote port resolves its host from the granted
Resource's name; `unbound_remote_config` raises rather than guessing. The reaper is therefore
per-resource, not fleet-wide: the sweep passes `resource_name` and the reaper binds with
`remote_config_for_resource`. This is deliberately unlike the fleet reapers in
`reaping/connections.py`, whose `config()` path raises precisely so they cannot sweep one host —
that shape exists for sweeps that have no row telling them which host to visit. This one has.

**Port** (`providers/infra/reaping.py`) — `CaptureReaper.reclaim_capture(capture)` taking a frozen
`OrphanedCapture(resource_name, domain_name, system_id, job_id)`. One method, because the two
libvirt calls must happen in one order on one connection, and the naming conventions belong to
the provider.

**Implementation** (`providers/remote_libvirt/reaping/capture.py`) — over one `qemu+tls://`
connection to the bound host: `object-del` `capture_qom_id(job_id)` on `domain_name`, then delete
`pcap_volume_name(system_id, job_id)`. Both tolerate not-found; a missing domain is not an error.
Order is load-bearing — deleting first is the unlinked-inode failure above. The sweep isolates
per row: a `reclaim_capture` failure is logged and skipped so one row never starves the pass,
matching `repair_leaked_domains` and `reap_orphaned_dump_volumes`.

`domain_name` comes from the query rather than being re-derived, because the handler that
attached the filter targets `system.domain_name or domain_name_for(system.id)`
(`capture_traffic.py:141`) — the stored column wins when set. Re-deriving would miss the filter on
any System with a stored name and then delete the volume anyway, reaching the unlinked-inode
outcome silently.

**Shared convention** — `capture_qom_id(job_id)` moves to `providers/ports/traffic.py`, which
already documents the `qom_id` contract, and the handler imports it instead of minting the
f-string inline. Both sides must produce the identical string. That is the only capture-path
change.

## Consequences

- A pcap left by a retry-exhausted, canceled, or worker-killed `capture_traffic` job is
  reclaimed, and the orphaned filter goes with it. For a worker-killed job this follows
  `repair_abandoned_jobs` dead-lettering the lease-lapsed, attempts-exhausted row to `failed`.
- A concurrent capture on the same System is untouched: the sweep addresses one exact
  `(system_id, job_id)` pair drawn from an already-terminal row, so a live capture — different
  `job_id`, `running` row — is never selected. No age heuristic stands between the sweep and a
  live capture, because the sweep cannot name one.
- Cost per pass is one unindexed `jobs` scan with four joins, plus one connection and two libvirt
  calls per selected row. No index is added: `repair_abandoned_jobs` already runs an unindexed
  per-pass `jobs` scan, and indexing one sweep and not its sibling would be arbitrary.
- The reconciler now reaches a domain monitor, not only storage. That is new reach for the
  reconciler against the remote host.
- **Residual: `settle` is a heuristic, not a derived bound.** The lease is heartbeat-renewed
  (`worker.py:155` rejects `heartbeat_interval > lease/3`), so a lapsed lease means the heartbeat
  *stopped* — a dead or wedged worker, off the nominal timeline by construction. Nothing bounds
  how long such a worker might still hold the volume, so no value is provably safe. 15 minutes is
  comfortably past a healthy capture (`CAPTURE_MAX_DURATION_S` is 300s) plus its fetch/store tail.
  Accepted because the capture being clobbered has already dead-lettered — its artifact is lost
  either way.
- **Residual: a row aging past `lookback` is never reclaimed, and kdive cannot detect it.**
  Nothing lists the `kdive-pcap-` prefix any more, so such a volume leaves no trace in the system
  and must be found by inspecting the storage pool. The sweep logs each per-row failure with its
  `(system_id, job_id)` and reports a reclaimed count per pass, which is what makes a *recurring*
  failure visible before it ages out; a row that silently ages out is not covered by that.
  Widening `lookback` is the remedy.

## Considered & rejected

- **List the storage pool and age on volume mtime** (the shape #1498 proposes, mirroring
  ADR-0094). Rejected on the Context evidence: an attached filter keeps the mtime fresh so the
  leak is never selected, and selecting it after an idle window unlinks an inode QEMU still
  writes to.
- **Extend `DumpVolumeReaper` to return both kinds.** Makes the port's name a lie, forces
  `reap_orphaned_dump_volumes` to branch across two different guards, and changes an ADR-0094
  contract for a reason unrelated to host_dump.
- **Do nothing; rely on `prepare`'s per-job pre-delete.** It only runs when that same `job_id`
  runs again, which a retry-exhausted job never does. This is the leak.
- **Reclaim at dead-letter time inside the job queue.** Couples the queue to provider storage and
  needs a working `qemu+tls://` connection at the moment of failure — often exactly when the host
  is unreachable. It also misses a worker that died without recording the transition, which is
  the case that leaves the filter attached.
- **Make the reaper fleet-wide, like the existing reapers, instead of threading `resource_name`.**
  Avoids the two extra joins, but the reaper would have to fan out per row to find the host —
  O(rows × hosts) connects per pass, worse than the pool-listing design it replaces, and against
  ADR-0187's rule that a per-op port binds to the granted Resource.
- **Add a `reaped` marker column to `jobs` instead of a lookback window.** A migration and new
  write traffic on the queue's hot table, to save a bounded scan the sibling sweep already pays
  unindexed. The lookback is weaker — a row older than the window is never revisited — and that
  is the trade accepted.
