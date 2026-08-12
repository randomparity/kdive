# Reap orphaned pcap capture volumes — design (#1498)

- **Issue:** [#1498](https://github.com/randomparity/kdive/issues/1498)
- **ADR:** [0555 — Reap orphaned pcap capture volumes with an owning-job guard](../adr/0555-reap-orphaned-pcap-capture-volumes.md)
- **Epic:** [#1423](https://github.com/randomparity/kdive/issues/1423) — remote-libvirt parity
- **Related:** ADR-0094 (host_dump volume reaper), ADR-0385 / ADR-0432 (traffic capture), #1434

## Problem

A `capture_traffic` job that exhausts its bounded retries leaves its pcap volume on the
remote host's storage pool with nothing left to reclaim it.

`RemoteLibvirtTrafficCapture` writes to `kdive-pcap-<system_id>-<job_id>.pcap` inside the
operator `storage_pool` (`traffic_capture.py:69-71`). Reclamation today has exactly two
paths, both owned by the job itself:

| path | code | fails when |
|---|---|---|
| handler `finally` reclaim around store | `capture_traffic.py:392` | the worker dies before `finally` runs |
| cancel-path reclaim | `capture_traffic.py:352` | the worker dies before it observes the cancel |
| `prepare` pre-delete of this job's own stale volume | `traffic_capture.py:197-211` | the job never runs again |

`JobState` (`domain/capacity/state.py:171-178`) has no dead-letter state: the retry edge is
`running → queued`, and exhausting retries lands the job in `failed`, a terminal state. A
terminal job never re-enters `prepare`, so its volume is never revisited.

`traffic_capture.py:22-24` already names the intended owner — "A pcap orphaned by a job that
exhausts its retries is reclaimed by the reconciler's volume reaper (a noted follow-up)".
This design implements that follow-up.

## Non-goals

Frozen from the issue charter:

- No change to local-libvirt traffic capture or any local-libvirt reaper.
- No change to the capture path (`prepare` / `attach` / `detach` / `fetch` / `reclaim`),
  beyond retiring the "noted follow-up" sentence in the module docstring.
- No change to host_dump volume reaping behavior (ADR-0094).
- No new agent-facing MCP tool or tool-schema change.
- No retry or dead-letter policy change in the job queue. Dead-lettering is the precondition
  this design handles, not the defect it fixes.

## Design

ADR-0555 records the decision and its rejected alternatives. The shape:

### 1. Port — `src/kdive/providers/infra/reaping.py`

```python
class PcapVolume(NamedTuple):
    name: str
    system_id: UUID | None
    job_id: UUID | None
    mtime_epoch_s: float

class PcapVolumeReaper(Protocol):
    async def list_pcap_volumes(self) -> list[PcapVolume]: ...
    async def delete_pcap_volume(self, name: str) -> None: ...

class NullPcapVolumeReaper: ...
```

Mirrors `DumpVolume` / `DumpVolumeReaper` / `NullDumpVolumeReaper`, including the idempotent
delete contract — a volume already gone is not an error, because a live capture's own
`finally` may remove it between the list and the delete.

`system_id` and `job_id` are `None` when the name does not parse, so an unparseable
`kdive-pcap-` volume stays age-reapable rather than immortal.

### 2. Implementation — `src/kdive/providers/remote_libvirt/reaping/pcap_volume.py`

`RemoteLibvirtPcapVolumeReaper`, a sibling of `RemoteLibvirtDumpVolumeReaper`. Reuses the
reaping package's existing seams unchanged:

- `map_over_fleet` for the list (isolates an unreachable host);
- `find_over_fleet` for delete-by-name (stops at the host that has the volume; not-found on a
  host is benign);
- `volume_mtime_epoch_s` for the `<target>/<timestamps>/<mtime>` parse.

Its own regex parses both UUIDs out of `kdive-pcap-<system_id>-<job_id>.pcap`.

### 3. Sweep — `src/kdive/reconciler/cleanup/provider_reaping.py`

```
reap_orphaned_pcap_volumes(conn, reaper, grace) -> int
```

Per volume, reap only when **both** hold:

1. `mtime_epoch_s < now() - grace`, with `now()` read from Postgres (never a Python clock —
   the DB clock is session-TZ-sensitive and is the reference the dump sweep already uses);
2. the owning job is not live — no `jobs` row with `id = job_id` in `queued` or `running`.

Guard 2 uses a new `is_job_live(conn, job_id)` helper beside `has_active_capture_job` in
`reconciler/repairs/allocations.py`, reusing that module's existing `_ACTIVE_JOB_STATE_VALUES`
so the "live" definition cannot drift between the two sweeps.

A `job_id` of `None` (unparseable name) skips guard 2 and relies on the age guard alone. Since
`volume_mtime_epoch_s` reads a timestamp-less document as epoch, such a volume is deleted on
the first pass; ADR-0555 records why that is accepted.

**Guard 1 is not redundant, and its window has a floor.** Guard 2 keys on the job row, not on
worker liveness. A `canceled` row precedes the worker's own reclaim by up to a poll interval,
and `repair_abandoned_jobs` dead-letters a lease-lapsed, attempts-exhausted `running` row to
`failed` while that worker may still be alive (`DEFAULT_LEASE` is 5 minutes;
`CAPTURE_MAX_DURATION_S` is 300s). In both windows only the age guard stands between the sweep
and a live capture's file, so `DEFAULT_PCAP_VOLUME_GRACE` must exceed `CAPTURE_MAX_DURATION_S`
plus the fetch/trim/store tail.

Per-volume delete failures are caught and logged, then the sweep continues — matching
`reap_orphaned_dump_volumes`.

### 4. Wiring

| file | change |
|---|---|
| `providers/remote_libvirt/composition.py` | `build_pcap_volume_reaper(*, secret_registry)` |
| `providers/assembly/composition.py` | `_pcap_volume_reaper_factories` + `build_reconciler_pcap_volume_reaper`, both mirroring the dump-volume pair and gated on `_remote_libvirt_enabled` |
| `reconciler/loop.py` | config fields `pcap_volume_reaper` (default `NullPcapVolumeReaper`) and `pcap_volume_grace` (default `DEFAULT_PCAP_VOLUME_GRACE`), plus a `reaped_pcap_volumes` repair-catalog entry |
| `providers/remote_libvirt/lifecycle/traffic_capture.py` | docstring only — replace "a noted follow-up" with the ADR-0555 reference |

`DEFAULT_PCAP_VOLUME_GRACE = timedelta(minutes=30)` is its own constant rather than an alias
of `DEFAULT_DUMP_VOLUME_GRACE`, so tuning one sweep cannot silently retune the other. 30
minutes clears the floor above (300s plus tail) with room.

## Threat model

The change deletes files on a remote host, so it gets a boundary pass even though it adds no
externally reachable entry point.

**Boundaries added.** One: the reconciler process now issues `storageVolLookupByName` +
`delete` against the operator `storage_pool` on each configured remote host, over the
existing mutual-TLS `qemu+tls://` fleet connection (ADR-0077). No new transport, credential,
or listener.

**Boundaries widened.** None. The reconciler already lists and deletes volumes in this pool
via `RemoteLibvirtDumpVolumeReaper`.

**Actor model.** The untrusted parties are (a) an MCP agent, which can start a
`capture_traffic` job and therefore influence *which* `job_id` appears in a volume name, and
(b) anyone with write access to the remote pool directory, which is the remote QEMU runtime
user. The design trusts the operator-configured fleet list and the remote host itself; a
compromised remote hypervisor is out of scope, as it is for every other remote-libvirt port.

**Control per boundary.**

| concern | control |
|---|---|
| deleting a live capture's volume | guard 2 — the owning job must not be `queued`/`running`; a concurrent capture has a different `job_id` and its own live row |
| deleting a volume that is not ours | the `kdive-pcap-` prefix plus the two-UUID regex; a foreign file in the pool never matches and is never listed |
| path traversal via a volume name | none needed — names come from libvirt's own pool listing and are passed back to `storageVolLookupByName`, not concatenated into a filesystem path. The delete is by libvirt volume name, not by path |
| an agent naming a volume to force deletion of another | `job_id` is the `jobs.id` primary key assigned by the queue (`capture_traffic.py:326`), not agent input; an agent cannot choose it |
| deleting the wrong host's volume | the name carries both UUIDs, so a cross-host collision would require the same job to exist on two hosts, which the System→host binding forbids |
| an unreachable host stalling the sweep | `map_over_fleet` already isolates per-host faults |

**Out of scope.** A compromised remote hypervisor that can forge volume names (it already
owns the pool). Deliberate operator deletion of a live volume. Denial of service by an agent
starting many captures — bounded by existing job admission, not by this sweep.

## Acceptance criteria

Sourced from issue #1498.

1. **A pcap volume left by a retry-exhausted `capture_traffic` job is reclaimed.** Test: a
   volume older than the grace window whose owning job row is `failed` → `delete_pcap_volume`
   called with that name; sweep returns 1.
2. **Reclamation does not race a concurrent live capture on the same System.** Tests:
   - a volume whose owning job is `running` → not deleted, even when older than grace;
   - a volume whose owning job is `queued` (between retries) → not deleted;
   - two volumes on one System, one job `failed` and one `running` → exactly the failed
     job's volume is deleted.
3. **Unit coverage of both directions**, per criterion 3 of the issue.

Additional cases the design's own guards require:

4. A volume newer than the grace window is not deleted even when its job is terminal.
5. A volume whose `job_id` names no row is deleted once past grace (GC'd job row).
6. An unparseable `kdive-pcap-` name is deleted once past grace, and never consults the job
   guard.
7. A `delete_pcap_volume` raising for one volume does not prevent the sweep reaping the rest,
   and the failure is logged.
8. An empty volume list short-circuits without querying the Postgres clock.
8b. `DEFAULT_PCAP_VOLUME_GRACE` exceeds `CAPTURE_MAX_DURATION_S`, asserted directly so a later
   tuning edit that drops it below the floor reddens rather than silently exposing live
   captures.
9. The name parser round-trips `pcap_volume_name(system_id, job_id)` — a property the two
   modules must agree on, tested against the real producer rather than a copied literal.
10. `just ci` is green.

## Verification

- Unit tests with fakes for the reaper and a real Postgres (testcontainers) for the sweep's
  job-liveness query, mirroring how `reap_orphaned_dump_volumes` is covered.
- The blocking libvirt calls in the new reaper stay `# pragma: no cover - live_vm`, matching
  `dump_volume.py`; orchestration and name parsing are unit-tested.
- No live proof is claimed. The remote `live_vm` tier (#1424) exists, but exercising a
  retry-exhausted capture against a real host is not something this change can force
  deterministically; the guard logic is where the risk is and it is unit-covered.
