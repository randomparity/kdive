# Reclaim orphaned pcap captures — design (#1498)

- **Issue:** [#1498](https://github.com/randomparity/kdive/issues/1498)
- **ADR:** [0555 — Reclaim orphaned captures from the job row, detaching the filter before the volume](../adr/0555-reap-orphaned-pcap-capture-volumes.md)
- **Epic:** [#1423](https://github.com/randomparity/kdive/issues/1423) — remote-libvirt parity
- **Related:** ADR-0094 (host_dump volume reaper), ADR-0385 / ADR-0432 (traffic capture), #1434

## Problem

A `capture_traffic` job that exhausts its bounded retries leaves two things behind on the remote
host: the pcap volume, and — if the worker died mid-capture — the QEMU `filter-dump` object still
bound to the live domain.

Reclamation today has three paths, all inside the owning job:

| path | code | fails when |
|---|---|---|
| handler `finally` reclaim around store | `capture_traffic.py:392` | the worker dies before `finally` runs |
| cancel-path reclaim | `capture_traffic.py:352` | the worker dies before it observes the cancel |
| `prepare` pre-delete of this job's own stale volume | `traffic_capture.py:197-211` | the job never runs again |

`JobState` has no dead-letter state: `running → queued` is the retry edge and exhaustion lands in
`failed`, terminal. A terminal job never re-enters `prepare`.

**The filter matters as much as the file.** `qom_id = f"kdive-dump-{job.id}"` is attached at
`capture_traffic.py:333` and removed only in the `finally` at `:350`. A worker killed between
them leaves it attached permanently — only a retry of that `job_id` would clear it, a different
capture uses a different `qom_id`, and `repair_leaked_domains` only touches domains whose System
row is gone.

This is why the pool-listing design first drafted for this issue was abandoned; ADR-0555 records
the evidence. An attached filter keeps the volume mtime fresh, so an age-based sweep never
selects the leak; and if the guest idles long enough to be selected, deleting the volume unlinks
an inode QEMU still holds — space unreclaimed, filter still writing, volume no longer listable.

## Non-goals

Frozen from the issue charter, as amended by the authorized rescope:

- No change to local-libvirt traffic capture or any local-libvirt reaper.
- No change to capture *behavior*. The one capture-path edit is moving `capture_qom_id` to
  `providers/ports/traffic.py` and importing it in the handler instead of inlining the f-string.
- No change to host_dump volume reaping behavior (ADR-0094).
- No new agent-facing MCP tool or tool-schema change.
- No retry or dead-letter policy change in the job queue.
- No schema migration, and no new index.

## Design

### 1. Shared convention — `src/kdive/providers/ports/traffic.py`

```python
def capture_qom_id(job_id: UUID) -> str:
    return f"kdive-dump-{job_id}"
```

The handler imports it in place of the inline f-string. Both the handler and the reaper must
produce the identical string; a duplicated literal is the drift this removes. `traffic.py` is the
right home — it already documents the `qom_id` contract and is importable by both without a
layering inversion.

### 2. Port — `src/kdive/providers/infra/reaping.py`

```python
class CaptureReaper(Protocol):
    async def reclaim_capture(self, system_id: UUID, job_id: UUID) -> None: ...

class NullCaptureReaper: ...
```

One method, not two: the detach and the delete must happen in that order on one connection, and
the naming conventions belong to the provider rather than the reconciler.

### 3. Implementation — `src/kdive/providers/remote_libvirt/reaping/capture.py`

`RemoteLibvirtCaptureReaper.reclaim_capture` opens one `qemu+tls://` connection over the existing
fleet helpers and, in order:

1. `object-del` `capture_qom_id(job_id)` on `domain_name_for(system_id)` — tolerating not-found
   and a missing domain;
2. delete volume `pcap_volume_name(system_id, job_id)` from the configured `storage_pool` —
   tolerating not-found.

Order is load-bearing (ADR-0555). Blocking libvirt calls stay `# pragma: no cover - live_vm`,
matching `dump_volume.py`.

### 4. Sweep — `src/kdive/reconciler/cleanup/provider_reaping.py`

```
reap_orphaned_captures(conn, reaper, *, settle, lookback) -> int
```

```sql
SELECT id, payload->>'system_id' AS system_id
FROM jobs
WHERE kind = 'capture_traffic'
  AND state = ANY(ARRAY['failed','canceled'])
  AND updated_at < now() - %(settle)s
  AND updated_at > now() - %(lookback)s
```

`succeeded` is excluded: the handler's `finally` runs before it returns and the worker marks the
row succeeded only after that, so its volume is already reclaimed. A row whose
`payload->>'system_id'` is absent or unparseable is skipped and logged — there is no volume name
to reconstruct without it.

Per-row failures are caught and logged, then the sweep continues, matching
`reap_orphaned_dump_volumes`. Time predicates run in Postgres, never a Python clock.

### 5. Wiring

| file | change |
|---|---|
| `providers/remote_libvirt/composition.py` | `build_capture_reaper(*, secret_registry)` |
| `providers/assembly/composition.py` | `_capture_reaper_factories` + `build_reconciler_capture_reaper`, mirroring the dump-volume pair, gated on `_remote_libvirt_enabled` |
| `reconciler/loop.py` | config fields `capture_reaper` (default `NullCaptureReaper`), `capture_settle` (`DEFAULT_CAPTURE_SETTLE`, 15 min), `capture_lookback` (`DEFAULT_CAPTURE_LOOKBACK`, 24 h), plus a `reaped_captures` repair-catalog entry |
| `providers/remote_libvirt/lifecycle/traffic_capture.py` | docstring only — replace "a noted follow-up" with the ADR-0555 reference |

## Threat model

**Boundaries added.** One: the reconciler now issues `object-del` against a running domain and
`delete` against a pool volume on each configured remote host, over the existing mutual-TLS
`qemu+tls://` fleet connection (ADR-0077). No new transport, credential, or listener. The
`object-del` is the genuinely new capability — the reconciler previously reached storage only.

**Boundaries widened.** The reconciler already deletes volumes in this pool via
`RemoteLibvirtDumpVolumeReaper`; the domain-monitor reach is new.

**Actor model.** The untrusted parties are (a) an MCP agent, which can start a `capture_traffic`
job and so influence which rows exist, and (b) whoever can write the remote pool directory (the
remote QEMU runtime user). The design trusts the operator-configured fleet list and the remote
host; a compromised remote hypervisor is out of scope, as for every remote-libvirt port.

**Control per boundary.**

| concern | control |
|---|---|
| detaching a live capture's filter | the row must be terminal *and* past `settle`; a live capture's row is `running` |
| deleting a live capture's volume | same — the sweep names one exact `(system_id, job_id)` pair from a terminal row and cannot name a live one |
| agent-chosen identifiers | `job_id` is the queue-assigned `jobs.id`, and `system_id` comes from the row's payload, not from a request field the agent controls at reap time |
| QMP injection via `qom_id` | `capture_qom_id` interpolates a `UUID` object, not a string; the command is built with `json.dumps`, not concatenation |
| path traversal via the volume name | none needed — the delete is by libvirt volume name in a configured pool, not a filesystem path |
| an unreachable host stalling the sweep | the existing fleet helpers isolate per-host faults |
| one bad row starving the rest | per-row try/except with a logged warning |

**Out of scope.** A compromised remote hypervisor that can forge volume names or QOM objects (it
already owns both). Denial of service by an agent starting many captures — bounded by existing
job admission. The residual in ADR-0555's Consequences: a heartbeat-stopped-but-alive worker may
still hold the volume past `settle`.

## Acceptance criteria

Sourced from issue #1498, criteria 1–3 verbatim.

1. **A pcap left by a retry-exhausted `capture_traffic` job is reclaimed.** A `failed` row older
   than `settle` and inside `lookback` → `reclaim_capture(system_id, job_id)` called; sweep
   returns 1.
2. **Reclamation does not race a concurrent live capture on the same System.** A `running` row is
   never selected; with two rows on one System, one `failed` and one `running`, only the failed
   job's pair is reclaimed.
3. **Unit coverage of both directions**, per criterion 3 of the issue.

From the design's own guards:

4. A `failed` row newer than `settle` is not selected.
5. A `failed` row older than `lookback` is not selected.
6. A `succeeded` row is never selected.
7. A `canceled` row *is* selected.
8. A row whose payload carries no usable `system_id` is skipped and logged, not raised.
9. One row's `reclaim_capture` raising does not prevent the sweep reclaiming the rest, and the
   failure is logged.
10. An empty row set short-circuits without calling the reaper.
11. **The reaper detaches before deleting**, asserted on call order against a fake — this is the
    unlinked-inode defect and ordering is the whole control.
12. `reclaim_capture` tolerates a missing filter, a missing volume, and a missing domain.
13. `capture_qom_id` has exactly one definition: the handler produces the same string the reaper
    reconstructs, asserted against the real producer rather than a copied literal.
14. `just ci` is green.

## Verification

- Unit tests with fakes for the reaper; a real Postgres (testcontainers) for the sweep's row
  selection, mirroring how `reap_orphaned_dump_volumes` is covered.
- Criterion 11 is an ordering assertion on a recording fake, not a live test.
- No live proof is claimed. Forcing a worker kill between attach and `finally` against a real
  host is not deterministically reproducible here; the risk is in the selection and ordering
  logic, which is unit-covered.
