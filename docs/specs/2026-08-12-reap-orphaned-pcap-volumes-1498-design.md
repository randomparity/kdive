# Reclaim orphaned pcap captures — design (#1498)

- **Issue:** [#1498](https://github.com/randomparity/kdive/issues/1498)
- **ADR:** [0555 — Reclaim orphaned captures from the job row, detaching the filter before the volume](../adr/0555-reap-orphaned-pcap-capture-volumes.md)
- **Epic:** [#1423](https://github.com/randomparity/kdive/issues/1423) — remote-libvirt parity
- **Related:** ADR-0094 (host_dump volume reaper), ADR-0187 (per-op resource binding), ADR-0385 / ADR-0432 (traffic capture), #1434

## Problem

A `capture_traffic` job that exhausts its bounded retries leaves two things on the remote host:
the pcap volume, and — if the worker died mid-capture — the QEMU `filter-dump` still bound to the
live domain.

Reclamation today has three paths, all inside the owning job:

| path | code | fails when |
|---|---|---|
| handler `finally` reclaim around store | `capture_traffic.py:392` | the worker dies before `finally` runs |
| cancel-path reclaim | `capture_traffic.py:352` | the worker dies before it observes the cancel |
| `prepare` pre-delete of this job's own stale volume | `traffic_capture.py:197-211` | the job never runs again |

`JobState` has no dead-letter state: `running → queued` is the retry edge and exhaustion lands in
`failed`, terminal. A terminal job never re-enters `prepare`.

**The filter matters as much as the file** — see ADR-0555 Context. It is why an mtime-aged pool
listing cannot work here, and why detach must precede delete.

## Non-goals

Frozen from the issue charter, as amended by the authorized rescope:

- No change to local-libvirt traffic capture or any local-libvirt reaper.
- No change to capture *behavior*. The one capture-path edit is moving `capture_qom_id` into
  `providers/ports/traffic.py` and importing it in the handler.
- No change to host_dump volume reaping behavior (ADR-0094).
- No new agent-facing MCP tool or tool-schema change.
- No retry or dead-letter policy change in the job queue.
- No schema migration and no new index.

## Design

### 1. Shared convention — `providers/ports/traffic.py`

```python
def capture_qom_id(job_id: UUID) -> str:
    return f"kdive-dump-{job_id}"
```

The handler imports it in place of the inline f-string at `capture_traffic.py:322`. Both sides
must produce the identical string; a duplicated literal is the drift this removes. `traffic.py`
already documents the `qom_id` contract and is importable by both without a layering inversion.

### 2. Port — `providers/infra/reaping.py`

```python
@dataclass(frozen=True, slots=True)
class OrphanedCapture:
    resource_name: str
    domain_name: str
    system_id: UUID
    job_id: UUID

class CaptureReaper(Protocol):
    async def reclaim_capture(self, capture: OrphanedCapture) -> None: ...

class NullCaptureReaper: ...
```

### 3. Implementation — `providers/remote_libvirt/reaping/capture.py`

`RemoteLibvirtCaptureReaper.reclaim_capture` binds the host with
`remote_config_for_resource(capture.resource_name)` (ADR-0187) — *not* the fleet bundle in
`reaping/connections.py`, whose `config()` raises by design because those sweeps have no row
telling them which host to visit. This one does.

Over one `qemu+tls://` connection, in order:

1. `object-del` `capture_qom_id(job_id)` on `capture.domain_name` — tolerating not-found and a
   missing domain;
2. delete volume `pcap_volume_name(system_id, job_id)` — tolerating not-found.

Order is load-bearing (ADR-0555). Blocking libvirt calls stay `# pragma: no cover - live_vm`.

### 4. Sweep — `reconciler/cleanup/provider_reaping.py`

```
reap_orphaned_captures(conn, reaper, *, settle, lookback) -> int
```

`capture_traffic` is Run-addressed — `CaptureTrafficPayload` has no `system_id` — so the sweep
resolves the System through the Run, exactly as the handler does:

```sql
SELECT j.id AS job_id,
       s.id AS system_id,
       COALESCE(s.domain_name, 'kdive-' || s.id) AS domain_name,
       res.name AS resource_name
FROM jobs j
JOIN runs rn       ON rn.id  = (j.payload->>'run_id')::uuid
JOIN systems s     ON s.id   = rn.system_id
JOIN allocations a ON a.id   = s.allocation_id
JOIN resources res ON res.id = a.resource_id
WHERE j.kind = 'capture_traffic'
  AND j.state = ANY(ARRAY['failed','canceled'])
  AND j.updated_at < now() - %(settle)s
  AND j.updated_at > now() - %(lookback)s
ORDER BY j.updated_at
```

`domain_name` is `COALESCE`d rather than re-derived because the handler targets
`system.domain_name or domain_name_for(system.id)` (`capture_traffic.py:141`) — the stored column
wins. A row whose `res.name` is NULL is logged at warning and skipped.

Per-row failures are caught, logged with the `(system_id, job_id)`, and skipped so one row never
starves the pass. Time predicates run in Postgres, never a Python clock.

### 5. Wiring

| file | change |
|---|---|
| `providers/remote_libvirt/composition.py` | `build_capture_reaper(*, secret_registry)` |
| `providers/assembly/composition.py` | `_capture_reaper_factories` + `build_reconciler_capture_reaper`, gated on `_remote_libvirt_enabled` |
| `reconciler/loop.py` | config fields `capture_reaper` (default `NullCaptureReaper`), `capture_settle` (15 min), `capture_lookback` (24 h), plus a `reaped_captures` repair-catalog entry |
| `providers/remote_libvirt/lifecycle/traffic_capture.py` | docstring only — replace "a noted follow-up" with the ADR-0555 reference |

## Threat model

**Boundaries added.** The reconciler now issues `object-del` against a running domain and `delete`
against a pool volume on the *bound* remote host, over the existing mutual-TLS `qemu+tls://`
connection (ADR-0077). The domain-monitor reach is the genuinely new capability; the reconciler
previously reached storage only.

**Actor model.** Untrusted parties are (a) an MCP agent, which can start a `capture_traffic` job
and so influence which rows exist, and (b) whoever can write the remote pool directory (the remote
QEMU runtime user). The design trusts the operator-configured inventory and the remote host; a
compromised remote hypervisor is out of scope, as for every remote-libvirt port.

| concern | control |
|---|---|
| detaching or deleting a live capture's state | the row must be terminal *and* past `settle`; a live capture's row is `running` and is never selected |
| agent-chosen identifiers | `job_id` is the queue-assigned `jobs.id`; `system_id`, `domain_name` and `resource_name` come from FK-joined rows, not request fields |
| QMP injection via `qom_id` | `capture_qom_id` interpolates a `UUID` object; the command is built with `json.dumps`, not concatenation |
| reaching the wrong host | `remote_config_for_resource` binds the granted Resource (ADR-0187); the sweep never fans out |
| path traversal via the volume name | the delete is by libvirt volume name in a configured pool, not a filesystem path |
| one bad row starving the rest | per-row try/except with a logged warning, and `ORDER BY updated_at` so retries are fair |

**Out of scope.** A compromised remote hypervisor that can forge volume names or QOM objects.
Denial of service by an agent starting many captures — bounded by existing job admission. The two
residuals in ADR-0555's Consequences (`settle` is a heuristic; a row aging past `lookback` is
undetectable by kdive).

## Acceptance criteria

Sourced from issue #1498, criteria 1–3 verbatim.

1. **A pcap left by a retry-exhausted `capture_traffic` job is reclaimed.** A `failed` row older
   than `settle` and inside `lookback` → `reclaim_capture` called with the right
   `OrphanedCapture`; sweep returns 1.
2. **Reclamation does not race a concurrent live capture on the same System.** A `running` row is
   never selected; with two rows on one System, one `failed` and one `running`, only the failed
   job's pair is reclaimed.
3. **Unit coverage of both directions**, per criterion 3 of the issue.

From the design's own guards:

4. A `failed` row newer than `settle` is not selected.
5. A `failed` row older than `lookback` is not selected.
6. A `succeeded` row is never selected; a `canceled` row is.
7. **Fixtures are built through the real payload type** — `CaptureTrafficPayload(...)`, not a
   hand-written dict — so a payload-shape regression reddens these tests instead of passing
   against a shape the enqueue path never writes.
8. `domain_name` uses the stored `systems.domain_name` when set, and the derived `kdive-<uuid>`
   only when NULL.
9. A row whose `resources.name` is NULL is skipped and logged, not raised.
10. One row's `reclaim_capture` raising does not prevent the sweep reclaiming the rest; the
    failure is logged with its `(system_id, job_id)`.
11. Selection is ordered by `updated_at`.
12. An empty row set short-circuits without calling the reaper.
13. **The reaper detaches before deleting**, asserted on call order against a recording fake —
    this is the unlinked-inode defect and ordering is the whole control.
14. `reclaim_capture` tolerates a missing filter, a missing volume, and a missing domain.
15. `capture_qom_id` has exactly one definition, asserted against the real producer.
16. `just ci` is green.

## Verification

- Unit tests with fakes for the reaper; a real Postgres (testcontainers) for the sweep's row
  selection and joins, mirroring how `reap_orphaned_dump_volumes` is covered.
- Criterion 13 is an ordering assertion on a recording fake, not a live test.
- No live proof is claimed. Forcing a worker kill between attach and `finally` against a real host
  is not deterministically reproducible here; the risk is in the selection, host binding and
  ordering logic, which is unit-covered.
