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

- the `capture_traffic` handler's `finally` reclaim, on every worker-driven exit;
- `prepare`'s pre-delete of *this job's own* stale volume, so an at-least-once retry starts
  clean;
- nothing else.

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
  `system_id=None, job_id=None` so it is still age-reapable rather than silently immortal.
- **Sweep** (`reconciler/cleanup/provider_reaping.py::reap_orphaned_pcap_volumes`) — a volume
  is reaped only when **both** guards pass:
  1. its store mtime is older than the grace window, measured against Postgres `now()`; and
  2. its owning job is not live — no `jobs` row with that id in `queued` or `running`.

  A volume whose `job_id` names no row is an orphan: the job row is created before the worker
  runs the handler that creates the volume, so a missing row means the row was garbage
  collected, never that the capture is about to start. The grace window still applies.

Both guards are required because they fail in opposite directions. The job guard is exact but
depends on a row that can be GC'd; the age guard is approximate but holds with no row at all.

## Consequences

- A pcap left by a retry-exhausted or worker-killed `capture_traffic` job is reclaimed on a
  later reconciler pass instead of leaking.
- A concurrent capture on the same System is not disturbed: its volume carries its own
  `job_id`, and that job is `running`, so guard 2 skips it. This is stronger than the
  System-scoped guard the dump sweep uses, and it is what makes concurrent captures safe.
- One added repair per reconciler pass, and one added storage-pool listing per fleet host per
  pass. `map_over_fleet` already isolates an unreachable host, so a down host degrades this
  sweep to a partial result rather than failing the pass.
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
  "is this job live?". Worth revisiting at a third kind.
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
