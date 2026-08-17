# 0562 — A per-System capture lease, taken under the lock the dump-volume sweep holds across its delete

## Status

Accepted (2026-08-17)

## Context

[ADR-0094](0094-remote-host-dump-via-coredump-volume.md) requires the orphaned host-dump volume
sweep and the capture path never to race over the deterministic per-System volume
`kdive-host-dump-<system_id>.kdump`. It named two guards: per-System capture serialization, and a
live-holder guard on the reap. [ADR-0557](0557-running-only-host-dump-live-holder.md) made the
second guard effective — the shipped predicate never matched — and disclosed that it remains a state
sample rather than an exclusion boundary, assigning the residual to #1955.

The residual is reachable at HEAD, and two independent gaps stack.

**The sweep samples and deletes with nothing held.** `reap_orphaned_dump_volumes`
(`reconciler/cleanup/provider_reaping.py`) reads `has_active_capture_job` and then calls
`delete_dump_volume` outside any transaction and outside any advisory lock. Its sibling
`repair_leaked_domains`, three functions above it in the same module, does take
`advisory_xact_lock(LockScope.SYSTEM, system_id)` around its equivalent decision. A worker can claim
the capture job in the gap: `has_active_capture_job` answers `false` for a `queued` job by design
(ADR-0557 rejected treating `queued` as a holder, because an unserved lane has no transition that
forces it terminal), the worker then commits `running`, and `capture_handler` proceeds to the
provider operation.

**The delete is addressed by name.** `RemoteLibvirtDumpVolumeReaper._delete_on_host`
(`providers/remote_libvirt/reaping/dump_volume.py`) does `storageVolLookupByName(name)` followed by
`volume.delete(0)`, with no re-read of the volume it sampled. The capture's own
`_delete_stale_volume` removes the prior orphan and `_core_dump` creates a **new** volume at the
same deterministic name (`providers/remote_libvirt/retrieve/host_dump_capture.py`), so the name the
sweep resolves after the claim is a different volume from the one it classified. ADR-0094's
30-minute mtime grace does not cover this: the sweep evaluated it against the old volume's
timestamp, and the fresh volume is never consulted.

The window therefore ends with `volume.delete(0)` unlinking a volume that `coreDumpWithFormat` is
writing or that `virStorageVolDownload` is streaming.

`capture_handler` (`jobs/handlers/artifacts/vmcore.py`) holds nothing across the provider
operation that names the System. `precheck_run` and `finalize_capture` take `LockScope.RUN`, and
ADR-0244 deliberately releases it before the capture so a multi-GiB stream is not held under a lock.
The one durable, System-independent declaration in that handler is the ADR-0502 write lease, minted
between `precheck_run` and `retriever.capture` over the Run's **object prefix** — it says nothing
about a libvirt volume.

A second state check would not close this. Any predicate the sweep evaluates before it deletes can
be falsified by a claim that lands after the evaluation, which is what the current code already
demonstrates. Mint-before-touch and classify-before-delete have to be totally ordered.

## Decision

Add a **host-dump volume lease**: a row a `capture_vmcore` job holds over its System while it
operates on that System's dump volume, minted under `LockScope.SYSTEM` — the lock the sweep now
holds across both its final classification and its delete.

1. **New table `host_dump_volume_leases`** (migration `0114`), PK `(system_id, job_id)`, both
   columns foreign keys with `ON DELETE CASCADE`. It carries no deadline column. Liveness is the
   holding job's, expressed by the existing `LIVE_HOLDER_SQL` from `artifacts/write_lease.py` —
   `jobs.state = 'running' AND jobs.lease_expires_at > now()`, the pair `dequeue` reclaims the
   complement of and `heartbeat` renews for as long as the handler runs. This is that constant's
   fourth reader after ADR-0502 and ADR-0522, and it is imported rather than restated for the reason
   ADR-0522 gives: a second copy of "the holder is still alive" would drift from the one the job
   runner enforces, and it would drift silently.

2. **The mint is `capture_handler`'s, after `hold_write_lease` and before `retriever.capture`.**
   It is gated on `CaptureMethod.HOST_DUMP`, the only method that creates the volume, so a
   `kdump`/`gdbstub`/`console` capture mints no row it does not need. It opens its own transaction
   and refuses a connection that is already in one (`require_top_level_transaction`): on a
   non-autocommit connection the block would degrade to a savepoint, leaving the row invisible for
   the whole operation it exists to fence and holding `LockScope.SYSTEM` until the handler returned.
   Both failures are silent at the call site, so the precondition is enforced rather than documented.

3. **The release is inside `finalize_capture`'s existing transaction**, alongside the write lease's,
   so a finished capture stops fencing in the same commit that registers its artifacts. The failure
   path deliberately does not release: a worker killed mid-capture releases nothing, so an `except`
   would be a fence that lifts only for the failures Python observed.

4. **The sweep runs its final classification and its delete inside one transaction holding
   `try_advisory_xact_lock(LockScope.SYSTEM, volume.system_id)`.** The mint takes the same lock, so a
   mint either precedes the classification — which then sees the row and skips the volume — or blocks
   until the delete has already happened, in which case the capture's own `_delete_stale_volume` finds
   nothing and `_core_dump` creates a volume the sweep has already passed. That ordering is the
   closure. The acquire is a `try`: a contended System is one a capture is declaring itself on right
   now, so the sweep skips that volume and the next pass re-derives it, rather than queueing a
   reconciler pass that has no deadline behind a foreground operation (the trade ADR-0502 item 4
   makes for the same reason).

5. **The classification keeps `has_active_capture_job` alongside the lease.** The two overlap for
   most of a capture's life — `dequeue` commits `running` before the handler runs, so ADR-0557's
   predicate already covers the whole interval from the claim to the last provider call — but neither
   is a superset of the other, and each covers a failure the other does not.

   The lease is keyed on the System. ADR-0557's predicate reaches the System only through
   `runs.id::text = jobs.payload->>'run_id'`, which is by design (`CaptureVmcorePayload` is
   Run-addressed and ADR-0557 rejected duplicating System identity into it), so a `runs` row deleted
   while its capture is in flight makes that predicate stop matching a live capture. The lease row
   does not depend on the join.

   ADR-0557's predicate, conversely, still matches a `running` job whose *job lease* has lapsed. That
   is precisely the state in which the lease row stops being live, and it is a worker the queue has
   given up on whose libvirt thread may still be writing.

   Losing either would widen the window rather than simplify the guard.

6. **`delete_dump_volume` becomes identity-addressed.** It takes the `mtime_epoch_s` the sweep
   sampled, and `_delete_on_host` re-reads the volume's `<target>/<timestamps>/<mtime>` from the
   volume it just looked up and returns without deleting when it differs. That re-read and the delete
   sit in the same lookup, so nothing can intervene between them. It is the backstop for the writer
   the lease cannot cover: a provider thread hung inside libvirt whose job has stopped being a live
   claim holds no lease and satisfies no state predicate, and the volume it recreated is the only
   evidence it exists.

   The port now returns whether **this call deleted the volume**, so `reaped_dump_volumes` counts
   what the sweep reclaimed and nothing else. False therefore covers two cases: the identity decline,
   and a name no reachable host held — the latter benign, but not a reap either. A decline still stops
   the fleet fan-out, because the host does hold that name and reporting "not mine" would send the
   sweep to another host's copy of the same deterministic name; the per-host step returns a three-state
   outcome so "handled the name" and "deleted the volume" stay separable.

7. **Stale leases are collected at the head of the sweep**, before it lists volumes, by the same
   `LIVE_HOLDER_SQL` the classification honours. A lease the collection has not reached already
   fences nothing, so this bounds table growth rather than exposure — and the growth is guaranteed,
   because every failed capture leaves a row. It is folded into `reap_orphaned_dump_volumes` rather
   than added to the reconciler's repair catalog: this sweep is the only reader of the table, it runs
   on every pass, and a new catalog entry would add a counter to the `ops.reconcile` response for a
   hygiene step with no operator decision attached to it. It runs before the early return for an empty
   volume list, so a deployment whose dump-volume reaper owns nothing still drains its leases.

8. **`_now_epoch` moves inside its own transaction.** It is the sweep's first statement, and on the
   reconciler's non-autocommit pool connection a bare `execute` opens a transaction that lives until
   the pool takes the connection back — after which every per-volume `conn.transaction()` would be a
   savepoint and every `pg_advisory_xact_lock` would be held for the whole pass. The per-volume blocks
   assert `require_top_level_transaction` for the same reason.

Lock ordering is unchanged. `LockScope.SYSTEM` precedes `LockScope.RUN` in the ADR-0016 total order,
and the handler acquires them in the opposite sequence — `precheck_run` (RUN), `hold_write_lease`
(RUN), the dump-volume mint (SYSTEM) — but never co-holds any two: each commits and releases before
the next begins. The sweep holds SYSTEM alone. `db/locks.py` records that this sequence is not a
co-hold and therefore not an ordering exception.

## Consequences

- A capture that transitions from queued to running while the sweep is mid-pass keeps its volume.
  Either the sweep's `try` fails and the volume is skipped, or the classification sees the lease, or
  the delete refuses on identity.
- An unowned stale volume is still reclaimed on the first pass that finds the System's lock free,
  which is every pass on which no capture is running.
- **The sweep now holds `LockScope.SYSTEM` across one provider delete, and the hold is not bounded by
  any timeout this tree configures.** For remote-libvirt that delete is a `find_over_fleet` walk
  opening a connection per declared host until one holds the volume, and
  `connection/transport.py`'s `open_libvirt_protocol` is a bare `libvirt.open(uri)` — there is no
  connect timeout anywhere in the tree. The worst case is therefore the operating system's TCP
  connect timeout (on Linux, `tcp_syn_retries`, ~130 s by default) once per unreachable declared
  host, not a value an operator can tune. That is the honest bound, and it is the same shape of trade
  ADR-0502 accepted in the other direction for `store.delete`, where botocore's five-attempt/60 s
  budget at least bounded it.

  The waiters are wider than the sweep's own scope. `services/runs/bind.py`'s `_bind_locked` blocks on
  `SYSTEM` **while already holding `ALLOCATION`**, so a bind that queues behind this hold also queues
  everything waiting on that allocation — `allocations.release` and the reconciler's
  expired-allocation repair among them. `repair_leaked_domains` and any other per-System operation
  wait directly. The sweep's own acquire is a `try`, so the sweep never joins such a queue; it only
  ever forms one.

  A `lock_timeout` would not help, for ADR-0502's reason: it bounds a waiter's wait, not a holder's
  hold. Bounding this properly means giving the remote-libvirt transport a connect timeout, which is
  a change to every remote-libvirt path rather than to this sweep, and is left to its own decision.
- **A System whose lock is held stays skipped, silently and per pass.** The skip is not counted as a
  fault, so a wedged lock holder defers that System's volume on every pass while the sweep reports a
  clean count. The per-skip INFO line naming the System is the whole of the signal, which is the
  drift hazard ADR-0455 §4 already discloses for its own skips.
- **A pool that reports no volume timestamps loses the identity backstop, and says so.**
  `volume_mtime_epoch_s` yields `0.0` for a volume whose XML carries no `<timestamps>`, and `0.0`
  compares equal to `0.0`, so the re-read cannot distinguish a recreated volume there. Such a pool has
  already lost ADR-0094's mtime grace for the same reason (`0.0` is always older than any cutoff), so
  the lease and the lock are its only guards — which is why item 6 is a backstop and not the mechanism.
  Because a guard that degrades in silence is one nobody knows to fix, `volume_mtime_or_warn` logs a
  WARNING naming the volume and both defeated guards on every such read. It does **not** refuse to
  reap: refusing would strand every genuine orphan on that pool forever, which is a worse failure than
  the one being disclosed.
- **A capture whose job lease lapses mid-operation loses its lease fence**, exactly as ADR-0502
  discloses for the write lease. `has_active_capture_job` still covers it while the row reads
  `running`, and the identity re-read still covers the volume it recreated.
- `jobs` becomes load-bearing for provider-state safety on one more path. A future change to how a
  running job's lease is represented has to consider this fence, as ADR-0502 already noted for the
  object store.
- One additive, forward-only migration (`0114`). One provider port signature change
  (`DumpVolumeReaper.delete_dump_volume` gains a required keyword argument and returns a bool), which
  has one production implementation and one null implementation. No configuration knob, no MCP
  tool-surface change, and no RBAC role change beyond the new table's grants — worker read/insert/
  delete, reconciler read/delete, server read-only, matching the shape every other ordinary table has.
  The `reaped_dump_volumes` counter keeps its meaning: a volume the sweep declined is not counted.

## Considered & rejected

- **Widen `has_active_capture_job` to include `queued`.** Reverses ADR-0557 on the grounds it
  rejected: an unserved queued job has no transition that forces it terminal, so it would pin a known
  orphan forever. It also still leaves a sample rather than a boundary — a job enqueued after the
  sample is not covered.
- **A boundary around the check and delete, with no identity check.** This is the closure for every
  writer that mints a lease, and it leaves the hung-provider-thread case: a job the queue has
  abandoned holds no live lease, so the classification passes and the name resolves to whatever that
  thread recreated. The identity re-read is what makes the delete refuse without needing to prove the
  writer is gone.
- **An identity check with no boundary.** Cheaper, and it fails on the pools that most need it: a
  volume whose XML carries no `<timestamps>` reads `0.0` on both samples and compares equal, so the
  delete proceeds. It would also be the second uncoordinated state check the issue rules out.
- **Hold a session advisory lock on a dedicated connection across the provider capture.** A session
  lock is released when its backend exits, which is the property that makes it wrong here: the failure
  this ADR must survive is a worker whose process is gone but whose libvirt operation is not, and a
  lock that vanishes with the process fences nothing at the moment the volume is most exposed. The
  durable row's liveness is the job lease, which a hung worker does not renew either — but the row
  stays until something deletes it, so the sweep decides on evidence rather than on a socket.
- **Hold `LockScope.SYSTEM` across the whole capture.** Reverses ADR-0244's reason for releasing
  `LockScope.RUN`: a host-dump capture spans a `coreDumpWithFormat` and a multi-GiB download, and a
  lock held across it blocks `runs.create` and every reconciler repair scoped to that System for the
  duration. The lease exists so a writer can declare itself without holding a lock across its work.
- **Reuse `object_write_leases` with a `systems` owner kind.** That table's `owner_kind` is an
  *upload* owner kind, its rows mean "objects are being written under this store prefix", and
  `lock_scope_for` maps them to the scopes the ADR-0455 orphan sweep locks. A libvirt volume is not a
  store prefix, and the sweep that reads that table would begin skipping keys for a System that has no
  keys.
- **Reuse ADR-0556's per-job session ownership fence.** That fence belongs to the supervised
  `capture_traffic` operation state machine (ADR-0559, #1951), where the lock-owning connection is
  also the operation's cancellation authority. The host-dump lane has no supervised operation row and
  no cancellation authority to attach one to; adopting the mechanism would mean adopting the state
  machine.
- **A lease with its own deadline column.** ADR-0502's rejection applies unchanged: the value would
  have to exceed the longest capture, nothing in the tree bounds one, and a value that is too small
  silently reopens this race.
- **Add the stale-lease collection as a reconciler catalog entry.** It would surface a counter in
  `ops.reconcile` for a step whose count an operator cannot act on, and this sweep is the table's only
  reader. See Decision item 7.
