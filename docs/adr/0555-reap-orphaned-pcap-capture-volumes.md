# 0555 — Reap orphaned pcap capture volumes with an owning-job guard

## Status

Accepted (2026-08-12)

## Context

`RemoteLibvirtTrafficCapture` (ADR-0432, #1434) writes each capture to a storage volume on
the remote host, named deterministically from the owning System and job:

```
kdive-pcap-<system_id>-<job_id>.pcap
```

Three paths reclaim it today, and all three depend on the owning job running again or
finishing:

- the `capture_traffic` handler's `finally` reclaim around store, on every worker-driven exit
  (`capture_traffic.py:392`);
- the cancel-path reclaim taken when the poll loop observes a canceled job
  (`capture_traffic.py:352`);
- `prepare`'s pre-delete of *this job's own* stale volume, so an at-least-once retry starts
  clean.

A job that exhausts its bounded retries never runs again. `JobState`'s
`running → queued` requeue is the retry edge, and retry exhaustion lands the job in `failed`
— terminal, with no further attempt. Its volume is therefore never revisited by `prepare`,
and if the worker died before the `finally` ran, never reclaimed at all. The pcap sits in the
operator's storage pool indefinitely.

This is the same class of leak ADR-0094 already sweeps for host_dump volumes, so the
reconciler is the right owner. What differs is the guard. The dump sweep asks a
System-scoped question — `has_active_capture_job(conn, system_id)` — because
`kdive-host-dump-<system_id>.kdump` encodes only the System. A pcap volume name encodes the
System *and the job*, and `capture_traffic.py:326` passes the `jobs.id` primary key, so the
exact owning row is addressable.

That distinction is load-bearing rather than incidental. Two captures can run concurrently on
one System; their volumes differ only by `job_id`. A System-scoped guard cannot tell them
apart, so it must either reap a live capture's volume or refuse to reap while any capture is
active on that System.

## Decision

We will add a `PcapVolumeReaper` port beside the existing `DumpVolumeReaper`, implement it for
remote-libvirt as a sibling class, and consume it from a new reconciler sweep.

- **Port** (`providers/infra/reaping.py`) — `PcapVolume(name, system_id, job_id,
  mtime_epoch_s)` plus `list_pcap_volumes()` / `delete_pcap_volume(name)`, mirroring the
  dump-volume port's shape and its idempotent-delete contract. `NullPcapVolumeReaper` is the
  default.
- **Implementation** (`providers/remote_libvirt/reaping/pcap_volume.py`) — a sibling of
  `RemoteLibvirtDumpVolumeReaper` reusing the package's existing fleet helpers
  (`map_over_fleet`, `find_over_fleet`) and `volume_mtime_epoch_s`. Its name regex parses
  both UUIDs; a `kdive-pcap-` volume whose name does not parse is reported with
  `system_id=None, job_id=None`. Such a volume has no job guard to consult, and
  `volume_mtime_epoch_s` reads a document with no `<timestamps>` as epoch, so it is deleted on
  the first pass rather than aged. That is accepted: the `kdive-pcap-` prefix is kdive-owned,
  `pcap_volume_name` is the only producer of parseable names, and the dir/fs pool traffic
  capture requires does populate `<timestamps>`.
- **Sweep** (`reconciler/cleanup/provider_reaping.py::reap_orphaned_pcap_volumes`) — a volume
  is reaped only when **both** guards pass:
  1. its store mtime is older than the grace window, measured against Postgres `now()`; and
  2. its owning job is not live — no `jobs` row with that id in `queued` or `running`.

  A volume whose `job_id` names no row is treated as an orphan, though this should not arise:
  `jobs` rows are never deleted (`db/schema/0038_runs_failing_job_id.sql` records that as a
  relied-upon invariant, and the only `DELETE FROM jobs` in the tree is scoped to
  `rootfs-reclaim` dedup keys). The age guard covers the case defensively.

`DEFAULT_PCAP_VOLUME_GRACE` is 30 minutes — its own constant, not an alias of
`DEFAULT_DUMP_VOLUME_GRACE`, so tuning one sweep cannot silently retune the other.

**The grace window has a floor, and guard 1 is not redundant.** Guard 2 keys on the job *row*,
which is not a liveness signal for the *worker*. Two paths take a `capture_traffic` row out of
`{queued, running}` while its worker still holds the volume open:

- **cancel** — the poll loop checks `_job_canceled` every `POLL_INTERVAL_SECONDS` (0.5s), so
  the row reads `canceled` for up to a poll interval plus detach before the worker's own
  reclaim runs;
- **lease lapse** — `repair_abandoned_jobs` dead-letters any `running` job with
  `lease_expires_at < now() AND attempt >= max_attempts` to `failed`, and the queue's fence
  design explicitly contemplates the reclaimed job's worker still being alive. `DEFAULT_LEASE`
  is 5 minutes against a `CAPTURE_MAX_DURATION_S` of 300s, so a stalled-but-alive worker on its
  final attempt lands here.

In both windows guard 2 passes and guard 1 is the only thing between the sweep and a live
capture's file. The pcap grace must therefore exceed `CAPTURE_MAX_DURATION_S` plus the
fetch/trim/store tail; 30 minutes clears that with room. Guard 1 also carries the unparseable
name case above, where guard 2 cannot be asked at all.

## Consequences

- A pcap left by a retry-exhausted or worker-killed `capture_traffic` job is reclaimed on a
  later reconciler pass instead of leaking. For a worker-killed job this follows
  `repair_abandoned_jobs` dead-lettering the lease-lapsed, attempts-exhausted `running` row to
  `failed`; until it does, guard 2 sees `running` and holds the reap off.
- A concurrent capture on the same System is not disturbed while its job row stays
  `queued`/`running`: its volume carries its own `job_id`, so guard 2 skips it. That is
  stronger than the System-scoped guard the dump sweep uses. Outside that window — a canceled
  or lease-lapsed row whose worker is still running — the grace floor above is what protects
  it.
- One added repair per reconciler pass, and per fleet host per pass a second `qemu+tls://`
  connect, a second `pool.refresh(0)`, and a second full `listAllVolumes` over the operator's
  storage pool. `map_over_fleet` opens a connection per host per call, so this is genuinely a
  doubled pool scan at the reconciler's cadence, not just an extra listing. It also isolates
  an unreachable host, so a down host degrades this sweep to a partial result rather than
  failing the pass.
- Per-volume delete failures are logged and skipped, so one bad volume does not starve the
  rest — the same posture as the dump sweep.
- `DumpVolumeReaper` and ADR-0094's host_dump behavior are untouched.
- Two reaper ports now exist where a future third volume kind would justify generalizing to
  one. That generalization is deliberately deferred; see below.

## Considered & rejected

- **Extend `DumpVolumeReaper` / `RemoteLibvirtDumpVolumeReaper` to return both kinds.** The
  issue offers this. It makes the port's name a lie, forces `reap_orphaned_dump_volumes` to
  branch per kind across two different guards, and changes an ADR-0094 contract for a reason
  unrelated to host_dump. The blast radius lands on the working sweep rather than the new one.
- **One generalized `OrphanedVolumeReaper` port with a kind discriminator.** This is the
  second volume kind, not the third. A discriminated union plus per-kind guard dispatch costs
  more than the roughly forty lines of fleet-walking it would save, and the two guards are
  genuinely different questions — a System-scoped "any capture active?" against an exact
  "is this job live?". The real cost this declines is runtime, not source: a single listing
  returning both kinds would avoid the duplicated per-host connect and pool refresh named in
  Consequences. That is accepted at the reconciler's default 30-second cadence against a pool
  that is already refreshed once per pass. Worth revisiting at a third kind, when the
  duplication triples.
- **Reuse the System-scoped `has_active_capture_job` guard.** It cannot distinguish two
  concurrent captures on one System, so it either reaps a live capture's volume or blocks
  reaping while any capture is active. It also asks about `capture_vmcore`, a different job
  kind entirely.
- **Do nothing; rely on `prepare`'s per-job pre-delete.** Pre-delete only runs when that same
  `job_id` runs again. A retry-exhausted job never does. This is precisely the leak.
- **Reclaim at dead-letter time inside the job queue.** Couples the queue to provider storage,
  needs a working `qemu+tls://` connection at the moment of failure — often exactly when the
  host is unreachable — and still misses a worker that died without recording the transition.
  The reconciler exists because state repair cannot depend on the failing actor.
- **Key the sweep on an owner tag in the volume metadata instead of the name.** libvirt
  storage volumes carry no general-purpose metadata field the way domains do; the
  deterministic name is already the ownership record, and ADR-0094 established that pattern.
