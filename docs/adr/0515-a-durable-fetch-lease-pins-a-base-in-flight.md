# 0515 — A deadline-bounded `rootfs_fetch_leases` row pins an uploaded rootfs base while its download is in flight

## Status

Accepted (2026-07-30)

## Context

#1558 offered two ways to stop a reclaim deleting a staged uploaded-rootfs base out from under a
live download. ADR-0495 implemented **option 1**: `_reclaim_one_checksum` probes for a
`<token>.*.partial` whose `flock` a live writer holds, and defers the checksum before any unlink.
That satisfied #1558's written acceptance criterion, and ADR-0495's own Consequences named three
residual windows it did not close.

#1702 carries **option 2**, for the substantive one — ADR-0495's *window 2*: a fetch that has
resolved its `artifacts` row but has not yet created its partial. At gate time there is no partial
to probe, so option 1 is blind to it, and the window is not short. `fetch_uploaded_rootfs` resolves
the row and *then* waits on the per-(investigation, checksum) session advisory lock, which can be
held for a sibling's entire multi-GiB download before this fetcher opens anything.

Nothing else closes it, and the reason is what ADR-0495 deferred on. The ADR-0441 §6 pin classifier
answers "does a System pin this base?" from the System's state column plus overlay-file presence,
and both terminal states a doomed provision reaches defeat it:

- `_ROOTFS_REFERENCERS_SQL` selects `WHERE investigation_id = %s AND state <> %s` with `torn_down`
  bound, so a `torn_down` System is never enumerated and cannot pin at all.
- `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` is `{PROVISIONING, REPROVISIONING, RESTORING}`. `FAILED`
  is absent, so a `failed` System pins only through the overlay-file stat — which returns false,
  because a System that died mid-fetch never got an overlay.

`PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` are both legal, the download runs detached
under `asyncio.to_thread` and cannot be cancelled, and nothing serializes the two: the fetch takes
only its own session lock, never the `INVESTIGATION` lock the reclaim holds.

ADR-0495 recorded why it did not simply widen the classifier: `torn_down` and `failed` carry no
"was provisioning" evidence, so option 2 needs **new durable state**. That is what this record adds.

## Decision

### 1. A `rootfs_fetch_leases` row, per holder (migration 0087)

A fetcher inserts a row naming `(investigation_id, token, system_id)` before it resolves its
`artifacts` row, and deletes it when the fetch returns or raises. The reclaim's per-checksum gate
asks whether any **unexpired** row exists for this `(investigation_id, token)`.

The row carries the **holder**, so two sibling Systems staging the same base each hold their own
lease and neither releases the other's. A `(investigation_id, token)` primary key would have had the
first sibling to finish delete the row and unpin a base the second was still downloading;
`object_write_leases` (ADR-0502) puts `job_id` in its PK for exactly this reason.

### 2. The lease is taken before `_resolve_object`, not after

The instant a fetch resolves its `artifacts` row it is a download a reclaim must not delete under.
Everything after that — the session-lock wait above all — is window 2. Taking the lease one line
later would leave that window open while looking correct, so the ordering is asserted as an
interleaved statement trace rather than as "a lease was taken at some point".

The lease therefore brackets ADR-0495's `flock` window rather than overlapping it. Both gates run,
in that order: the lease first because it is the cheaper question (one indexed `EXISTS` on a
connection already open, against a `scandir` of the staging tree) and the wider one.

### 3. `expires_at` is load-bearing, and it is what keeps AC-8's property true

This design cannot borrow ADR-0502's fence. That lease could decline a deadline entirely because its
liveness *is* its holding job's: `jobs.state = 'running' AND jobs.lease_expires_at > now()`, renewed
by the worker heartbeat. A rootfs fetch has no such handle — the provision seam hands it a
`RootfsUploadContext` with no job identity, and `UploadFetch` is a bare `(ctx) -> Path` callable — so
there is nothing to fence on without plumbing a job id through the provider seam.

Absent that, a fetcher killed by `SIGKILL` releases nothing, and **nothing else ever clears its
row**: `failed` is terminal with no transition out of it, `torn_down` is the achieved post-state, and
no reconciler repair reaches a lease. A bare existence test would therefore pin the base forever on
precisely the path that matters — the disk-exhaustion regression
`test_failed_referencer_with_overlay_gone_drains` (AC-8) exists to catch. The deadline is the only
thing standing between this design and that regression, so it is evaluated inside the same statement
as the existence test rather than by a caller who might forget it.

`expires_at` is set from Postgres `now()` at acquire and compared against Postgres `now()` at the
gate, so no worker clock enters the comparison — this tree's `now()` is session-TZ rather than UTC,
and a Python-side deadline against a drifting worker clock would expire a live lease early on
exactly the hosts where the drift is worst.

### 4. The TTL is 6 hours, derived

| step | figure |
|---|---|
| canonical per-object cap (`KDIVE_MAX_UPLOAD_BYTES` default) | 50 GiB |
| floor sustained staging throughput | 5 MiB/s |
| one full-cap transfer at the floor | 10,240 s ≈ 2 h 51 m |
| the lease also covers one session-lock wait behind a sibling that fails without publishing | × 2 ≈ 5 h 41 m |
| rounded up | **6 h** |

The floor rate is chosen for an asymmetry, not measured: it sits about an order of magnitude below
what a healthy host achieves against a same-LAN S3-compatible store, because the TTL must not be the
thing that fires on a slow-but-working transfer. Expiring under a live fetcher silently reopens the
very race this lease closes, which is the worst failure a fence can have (ADR-0502's own words about
its rejected deadline). Erring long costs a bounded, visible leak; erring short costs correctness.

The ×2 is not padding. The lease is deliberately acquired *before* the session lock — that is §2, and
the whole reason it sees window 2 — so a fetcher legitimately holds it through a sibling's full
transfer and then its own.

### 5. The reap is hygiene, not correctness

`reap_expired_fetch_leases` deletes an investigation's expired rows from the reclaim job, which
already holds that investigation's `INVESTIGATION` advisory lock and is walking its checksums. No
reconciler lane is added. An expired row is already inert to the gate, so nothing observable depends
on the reap having run — it exists so a host that repeatedly kills fetchers does not accumulate dead
rows for the life of the investigation. Both foreign keys are `ON DELETE CASCADE`, which covers the
investigation- and System-deletion paths.

### 6. The state classifier is not widened

`domain/capacity/state.py` is unchanged. `pinned_rootfs_tokens`, `rootfs_base_reclaimable` and
`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` all keep their existing semantics, so:

- **`test_failed_referencer_with_overlay_gone_drains` (AC-8) is unchanged and still passes.** A
  `failed` System with no fetch in flight drains exactly as it did.
- **`test_torn_down_referencer_is_excluded` is unchanged and still passes.**

Both were reconciled by keeping the new evidence out of the classifier.
`test_the_state_classifier_is_not_widened_by_the_fetch_lease` is added beside them: it holds a live
lease for a failed System's own token and asserts `rootfs_base_reclaimable` still says
"reclaimable", because answering the liveness question is not the classifier's job. A later
"simplification" that adds `FAILED` to the pre-overlay set now reddens next to AC-8.

## Consequences

The substantive residual ADR-0495 recorded is closed: a reclaim can no longer delete a staged base
for a checksum whose download has resolved its `artifacts` row but not yet created its partial.

**The new residual, stated plainly: a fetcher killed by `SIGKILL` pins its base for up to 6 hours.**
Its staged base, its object and its `artifacts` row are all retained until the lease expires, and
the first reclaim pass after that drains the checksum normally. This is the accepted cost of holding
the evidence in the database rather than in something the kernel releases at process exit. It is
bounded, it is visible in `rootfs_fetch_leases`, and it is reclaimed without operator action —
but for those 6 hours the disk is not returned. An operator who needs the space sooner can delete
the row; it is inert state, not a lock.

Two older residuals remain unchanged: the sub-syscall instant between the last probe and the base
unlink, and a partial the `flock` probe cannot evaluate at all (`EACCES`, `ENOLCK`, `EOPNOTSUPP`),
where the reclaim proceeds as it did before ADR-0495 per ADR-0452 §5.

A lease acquire that faults degrades to an unleased fetch with a `WARNING` rather than failing the
provision — the reclaim reverts to its pre-ADR-0515 reach, which is a rare and survivable race,
where failing would turn any transient database blip into a total uploaded-rootfs provisioning
outage. A release that faults is likewise reported and left to the TTL; it must not raise, because
raising out of the `finally` would demote an actionable `CategorizedError` to `__context__` behind a
Postgres message, which is the defect `_release_fetch_lock` documents one call away for the advisory
lock.

Every provision of an upload-rootfs System now costs two extra statements on the fetch's existing
connection, and every due checksum one indexed `EXISTS` on the reclaim's. The lease table's live set
is bounded by concurrent fetches; its dead set by the reap.

## Considered & rejected

**A `flock`-held `<token>.<uuid>.fetching` lease file.** Implemented first, and rejected by the
operator in favour of durable database state. The argument for it is recorded here rather than lost,
because it is the honest alternative and the next reader will think of it: an `flock` is released by
the kernel when the holding descriptor closes, **including on `SIGKILL`**, so the pin cannot outlive
its holder and no TTL is needed — which removes this record's one residual entirely, along with the
question of what the TTL value should be. It is also the conclusion this subsystem had already
reached three times (ADR-0446, ADR-0452, ADR-0495), and the reclaim already depends on the
worker/staging co-location that would make the file readable (ADR-0442). Against it: the evidence
lives outside the state of record, so it is invisible to any operator query, to any future
non-co-located reclaim, and to anything that is not the worker holding that filesystem. The operator
weighed both, with the objections above in front of them, and chose the database. That is a decision
about where this system's durable state belongs, not a finding about `flock`.

**Fence on the holding job instead of a deadline** — ADR-0502's own answer for a structurally
identical lease, and strictly better where it applies: a heartbeat-renewed
`jobs.lease_expires_at` needs no derived constant and no residual leak window. Rejected **for this
change only**, on scope: the provision seam passes the fetch a `RootfsUploadContext` carrying no job
identity, so adopting it means widening `UploadFetch` and every runtime that builds one. This is the
natural follow-up if the 6-hour window proves too coarse in practice, and it would delete §4 rather
than tune it.

**Add `FAILED` to `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`** — the edit the issue title suggests, and
a one-line change. Rejected: `failed` is terminal with nothing that transitions out of it and
nothing that removes such a System's overlay, so the pin would never release and the base would leak
until an operator intervened. It also answers the wrong question — a terminal state says nothing
about whether a download is in flight, which is why ADR-0495 deferred option 2 rather than taking
this shortcut. AC-8 is the standing guard against it.

**Include `torn_down` in `_ROOTFS_REFERENCERS_SQL`** — the same defect from the other side, and
worse: `torn_down` is the achieved post-state of every System that ever existed, so every
investigation would accumulate permanent pins.

**A `systems.rootfs_fetch_started_at` column instead of a table.** Rejected on the same collision
§1 describes: the column is per-System, but the thing being pinned is a per-(investigation, checksum)
base that several Systems share, so N sibling Systems fetching one base would write N rows whose
clearing order decides whether the pin survives. It also puts a hot write on `systems`, a table
every bind reads, for state that is transient by construction.

**A session `pg_advisory_lock` as the pin** — appealing, since the fetch already takes one.
Rejected: ADR-0446 established that a session lock belongs to a Postgres *connection* which sends
nothing for the whole download and can be reaped by an idle-connection timeout or a terminated
backend. It would release the pin while the writer is still writing — the failure this record
exists to prevent, and one that leaves no evidence behind.

**Drop ADR-0495's `flock` probe now that the lease is wider.** Rejected: across a rolling worker
upgrade a fetcher started before this change is mid-download with a partial and no lease, so
dropping the older evidence would reclaim its base out from under it and reintroduce #1565 for the
length of the deploy.

## References

- Issue #1702 (this record), #1558 (option 2 as originally stated), #1565, #1544, #1522
- ADR-0495 — reclaim defers a live-held checksum (option 1; its Consequences name window 2)
- ADR-0502 / migration 0084 — `object_write_leases`; the per-holder PK, and the deadline it could
  decline because it had a job to fence on
- ADR-0446 — why a session advisory lock is not liveness
- ADR-0452 §5 — what may and may not pin a drain
- ADR-0441 §5/§6 — staging concurrency and the pin classifier
- ADR-0442 — reclaim ordering and the worker/staging co-location
- ADR-0015 — migrations are additive and forward-only
