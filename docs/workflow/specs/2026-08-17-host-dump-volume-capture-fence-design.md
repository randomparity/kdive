# Host-dump volume capture fence design (#1955)

## Scope

Close the queued-to-running race between the ADR-0094 orphaned host-dump volume sweep and
`capture_vmcore`'s provider operation. The sweep must not delete the volume of a capture that is
claimed after the sweep classifies it, and must still reclaim a volume no capture owns.
[ADR-0562](../../adr/0562-host-dump-volume-capture-lease-fence.md) records the concurrency and
connection-lifetime decision.

Out of scope: traffic-capture reclamation (ADR-0555/0556/0559), the queue's retry and lease policy,
`CaptureVmcorePayload`'s Run-addressed shape, MCP tool contracts, and the `reaped_dump_volumes`
counter's meaning.

## Design

### The lease

`host_dump_volume_leases (system_id, job_id, created_at)`, PK `(system_id, job_id)`, both identity
columns `ON DELETE CASCADE` foreign keys, in migration `0114`. No deadline column: liveness is
`artifacts/write_lease.py`'s `LIVE_HOLDER_SQL`, imported rather than restated.

`providers/shared/host_dump_volume_leases.py` holds the three statements, beside
`providers/shared/rootfs_fetch_leases.py` for the same reason that module gives: the writer is a job
handler and the reader is the reconciler, and the subject is provider state rather than an artifact.

- `hold_host_dump_volume_lease(conn, system_id, job_id)` — asserts
  `require_top_level_transaction`, then inserts under `advisory_xact_lock(SYSTEM, system_id)` in its
  own transaction. `ON CONFLICT DO NOTHING`, so a retried attempt of the same job is a no-op.
- `release_host_dump_volume_lease(conn, system_id, job_id)` — one `DELETE`, no lock and no
  transaction of its own; the caller supplies both.
- `has_live_host_dump_volume_lease(conn, system_id)` — `EXISTS` over the System's rows joined to
  `LIVE_HOLDER_SQL`.
- `reap_stale_host_dump_volume_leases(conn)` — deletes every row with no live holder; returns the
  count.

### The handler

`capture_handler` mints after `hold_write_lease` and before `resolver.binding_for_system`, gated on
`method is CaptureMethod.HOST_DUMP`. `finalize_capture` releases inside its existing per-Run
transaction, beside `release_write_lease`. The failure path releases nothing.

Both existing acquisitions are `LockScope.RUN` and each commits before the next statement runs, so
the mint's `LockScope.SYSTEM` is a fourth sequential acquisition and not a co-hold. `db/locks.py`'s
`LockScope` docstring records that.

### The sweep

`reap_orphaned_dump_volumes` becomes:

1. `reap_stale_host_dump_volume_leases` — before the volume list, so an empty list still drains.
2. `reaper.list_dump_volumes()`; return `0` on an empty list.
3. `_now_epoch` inside its own transaction, so the connection returns to idle and the per-volume
   blocks are real transactions rather than savepoints.
   `require_top_level_transaction` is asserted at the head of the function as well as per volume,
   because step 1 runs before the volume list and would otherwise degrade to a savepoint that commits
   nothing on a connection already in a transaction — and with an empty list the pass would return `0`
   without ever reaching the per-volume assertion.
4. Per volume, one transaction asserting `require_top_level_transaction`:
   - skip when the sampled mtime is at or after the cutoff (unchanged);
   - when the volume names a System: `try_advisory_xact_lock(SYSTEM, system_id)`, skipping the volume
     when it is contended; then skip on `has_live_host_dump_volume_lease` or
     `has_active_capture_job`;
   - `reaper.delete_dump_volume(volume.name, expected_mtime_epoch_s=volume.mtime_epoch_s)`, inside
     the same transaction so the lock is still held;
   - a per-volume delete failure is logged and isolated, as today.

A volume whose name carries no parseable System UUID takes no lock and keeps its age-only
classification; the identity argument still applies to its delete.

### The provider port

`DumpVolumeReaper.delete_dump_volume(name, *, expected_mtime_epoch_s: float) -> bool`, where the
return means "this call deleted the volume". False covers both an identity decline and a name no
reachable host held; neither reclaimed anything, and `reaped_dump_volumes` counts reclamation.

`RemoteLibvirtDumpVolumeReaper._delete_on_host` re-reads `volume.XMLDesc(0)` through
`volume_mtime_epoch_s` after `storageVolLookupByName` and skips the delete when the value differs
from `expected_mtime_epoch_s`. It returns a three-state `_HostOutcome` rather than the fan-out's
bool, because "this host handled the name" and "the volume is gone" stop being the same fact once a
delete can decline: a decline must stop the fan-out — otherwise the sweep goes looking for another
host's copy of the same deterministic name — while still reporting nothing reclaimed.

## Failure behavior

- A contended System lock defers that volume to the next pass; it is not a counted fault.
- An identity mismatch is an INFO line naming the volume and both mtimes, not an error: the volume the
  sweep classified is gone and the one present is not its business. It is not counted as reaped.
- A volume reporting no `<target>/<timestamps>/<mtime>` gets a WARNING from `volume_mtime_or_warn`
  naming the volume and the two guards `0.0` defeats, on every read. The reap still proceeds — refusing
  would strand every genuine orphan on such a pool.
- A lookup that reports `VIR_ERR_NO_STORAGE_VOL` remains benign, as today.
- A `libvirtError` from the re-read is wrapped as `INFRASTRUCTURE_FAILURE` like the existing lookup
  and delete failures, and the sweep's per-volume `except` isolates it.
- A lease whose `jobs` or `systems` row is deleted goes with it via `ON DELETE CASCADE`.

## Verification

Database-backed and unit tests, all in the default (non-`live_vm`) tier:

1. **The race test** (`tests/adversarial/test_host_dump_volume_capture_fence.py`). A queued capture
   job and an old volume for its System. The sweep is suspended inside the port's
   `delete_dump_volume`, i.e. after its classification. The test then performs the real
   queued-to-running transition — `queue.dequeue` followed by `capture_handler` — and then waits for
   whichever of two states the tree under test reaches, both stable once true: the handler's mint
   blocked on `(SYSTEM, system_id)`, read from `pg_locks` on a third connection, or the provider call
   reached with nothing declared. Releasing the sweep then lets it act. The assertions are
   behavioural — the capture's volume is present at the end of the pass, and the sweep deleted only
   the identity it sampled — so against unfixed code the test fails on the destroyed volume rather
   than on a wait, and against the fix neither branch is a timing bet.
2. **The fence, without the interleaving.** A claimed capture holding a live lease, suspended
   mid-provider-operation: the sweep reports `0` and deletes nothing even though the sampled mtime is
   an hour old. The falsifier arm removes the holders from an outside session and runs the same pass
   against the same volume, which reaps it — so the refusal is a refusal and not a no-op. It removes
   **both** the lease row and the `running` job state, because the two holders are independent and
   either alone defeats the arm; what the lease contributes that the job-state predicate cannot is
   isolated separately in `tests/reconciler/test_loop.py`, against a volume whose System the running
   job's Run is not bound to.
3. **The mint's visibility and the lock's freedom**, sampled from an independent connection while the
   provider operation is in flight, mirroring `tests/adversarial/test_vmcore_capture_write_lease.py`:
   a savepoint mint inserts the row, raises nothing, and fences nothing.
4. **The release and the reap.** A successful capture leaves no lease; a failed one leaves its lease
   and `reap_stale_host_dump_volume_leases` collects it. A `kdump` capture mints no lease.
5. **The identity-addressed delete** (`tests/providers/remote_libvirt/retrieve/test_dump_volume_reaper.py`).
   The existing fakes drive `_delete_on_host`: a matching mtime deletes and reports `True`, a changed
   mtime does not and reports `False`, the fan-out stops at the host that held the volume either way,
   an absent volume is benign but is not a reap, and a failed identity re-read is an infrastructure
   fault. A volume reporting no `<timestamps>` warns exactly once; one reporting a usable timestamp
   warns not at all.
6. **The port contract holds for the doubles.** `isinstance(double, DumpVolumeReaper)`, so a double
   cannot quietly re-admit the defaulted `expected_mtime_epoch_s` the port requires.
7. **The sweep's own behaviour** (`tests/reconciler/test_loop.py`): the contended-lock skip, the
   live-lease skip and its lapsed-holder falsifier, the stale-lease collection running on an empty
   volume list, the sampled identity reaching the port, a declined volume not being counted as
   reaped, the head transaction guard and its transaction-free counterpart, and the existing
   grace-window, terminal-job, iteration and per-volume failure-isolation tests.
8. `env -u FORCE_COLOR just ci` bare.

Each new guard is mutation-verified by breaking it and confirming the test reddens, with
`__pycache__` cleared before re-confirming the restored tree is green.

## Repository context

- Branch: `feat/fence-host-dump-capture-1955`
- Base branch: `main`
- PR guardrail: `env -u FORCE_COLOR just ci`
- Assigned numbers: ADR `0562`, migration `0114`
- No architecture-sensitive generation. Retained context: host `x86_64`, declared targets `x86_64`
  and `ppc64le`, relationship `included`.
