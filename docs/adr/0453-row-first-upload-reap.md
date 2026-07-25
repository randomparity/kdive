# ADR 0453 — Commit the upload manifest-row delete before sweeping its objects

- **Status:** Accepted
- **Date:** 2026-07-25
- **Amends:** [ADR-0448](0448-enforce-upload-deadline-at-run-finalize.md) §2's **first** recorded
  residual — the partially-completed reap — which is the defect fixed here. ADR-0448's decisions
  and its other two residuals are retained unchanged.
- **Depends on:** [ADR-0048](0048-external-build-artifact-ingestion.md) §6 (the `upload_manifests`
  window and its reaper), [ADR-0104](0104-chunked-external-upload-reassembly.md) §7 (the
  leftover-chunk backstop the runs branch generalizes),
  [ADR-0444](0444-enforce-upload-deadline-at-investigation-finalize.md) (deadline-alone reaping for
  the investigations lane).
- **Spec:** [`../specs/2026-07-25-row-first-upload-reap-1552-design.md`](../specs/2026-07-25-row-first-upload-reap-1552-design.md)

## Context

`reap_one_owner` deletes a past-deadline upload window's S3 objects **inside** the transaction that
deletes its `upload_manifests` row. The object deletes are immediate and irreversible; the row
delete commits only at transaction exit. An abort after the loop begins — a `CategorizedError` out
of `store.delete`, a lost connection, cancellation at shutdown — rolls the row back with the bytes
already gone. The delete is a per-object loop, one `delete_object` per key, so partial failure is
the normal shape of an object-store outage here rather than an exotic edge.

The restored row is byte-identical to the window that existed before the reap: same `prefix`, same
`deadline`. That identity is what makes it dangerous. ADR-0448 §2's `_require_unreaped_window`
re-reads the manifest under the `RUN` lock and compares the validated window's deadline stamp —
which correctly rejects a **committed** reap (row absent) and a **re-mint** (deadline re-stamped),
and cannot see a **rolled-back** reap at all. The single-PUT finalize therefore proceeds and
registers `artifacts` rows against deleted keys, marking the Run `succeeded` with a dangling
`kernel_ref`. That is the exposed lane: it needs a finalize straddling the deadline concurrent with
a failing reap, which is narrow but reachable.

Two claims in the issue that motivated this ADR do not hold, and the decision does not rest on
either. First, a row deleted with objects present is **not** a leak #768's reaper already handles:
`gc_expired_build_artifacts` is row-driven over `artifacts`, and this reaper by construction only
deletes objects that have no `artifacts` row, so a row-first orphan is structurally invisible to it;
the only prefix-driven orphan scan covers `images/`, not the upload prefix, and there is no S3
lifecycle rule in the tree. Second, the **investigations** lane is not exposed to this defect,
though it shares this one function: `complete_rootfs_upload` does its HEAD, its `artifacts` write,
and its `delete_manifest` inside one transaction under the same `INVESTIGATION` lock the reaper
takes, so it runs wholly before or wholly after the reap transaction and never observes a rollback.
That exemption is specific to the defect being fixed and does **not** carry over to the residual
this change introduces — see §Consequences, where the same lane is in scope.
The asymmetry still favours row-first — silent corruption of a `succeeded` Run against unreferenced
bytes — but it is a trade against an **unswept** leak, and §Consequences states that plainly rather
than borrowing coverage that does not exist.

## Decision

### 1. Split the reap on the commit

`reap_one_owner` becomes a driver over two phases.

**Phase 1** keeps the existing transaction and per-owner advisory lock and does every decision that
needs the database: re-read the past-deadline manifest (declining a renewed or absent one), list the
prefix, subtract the keys holding a committed `artifacts` row, `DELETE` the manifest row. It returns
the doomed key set; the transaction commits on exit.

**Phase 2** takes only the store and that key list — no connection, no transaction, no lock — and
deletes each key.

The committed-object exemption is unchanged in effect and is now computed with one set-valued
`object_key = ANY(%s)` query instead of one query per key, which also shortens the locked phase.

### 2. The committed row delete is the barrier against the reaped window's own finalizes

Computing the exemption before the split is safe **against the two finalizes** because neither can
run for the reaped window once phase 1 commits: both require the manifest row phase 1 just deleted —
`_require_unreaped_window` rejects with `no_upload_manifest`, `complete_rootfs_upload` rejects for a
missing manifest. This is the substance of the decision, not merely a different order: object-first
has no such barrier available, because the state that authorizes the deletes is only durable once
the row delete commits.

The barrier reaches exactly that far, and this ADR states its limits rather than asserting it is
absolute. Two classes of writer sit outside it, both because phase 2 no longer holds the owner lock
that used to span the check and the delete:

- a **re-mint** creates a *new* manifest row, lifting the barrier for the window it opens; upload
  keys are owner-addressed, so that window reuses these same key names;
- `local/runs/<id>/` is the Run's **owner** prefix, not an upload-only namespace, and phase 1 dooms
  everything `list_prefix` returns under it. Other run-scoped writers put objects there and commit
  `artifacts` rows for them under no manifest at all — `control.capture_traffic`'s
  `pcap-<job_id>`, whose PUT and row insert share one `RUN`-locked transaction, and the vmcore
  rows, whose objects are PUT by the provider well before `finalize_capture` inserts them.

`capture_traffic` is **newly** exposed: its orphan-then-retry path (a prior attempt whose PUT
survived a rolled-back transaction, re-PUT under the same key on retry) could not interleave while
the reaper held the `RUN` lock across the per-key check and delete. The vmcore exposure predates
this change, since its object already existed rowless outside any lock. Both are residual 2 in
§Consequences, filed as #1557, and `test_a_key_that_gains_an_artifacts_row_after_the_claim_is_
still_deleted` pins the behaviour so the residual has a reproducer.

### 3. A failed key strands nothing, and the pass reports it at the end

Phase 2 catches `CategorizedError` per key, logs it at WARNING with the key, and continues,
returning how many it could not delete; `reap_one_owner` then logs a non-zero count once at ERROR
naming the owner and the totals. Raising *there*
would be worse on every axis: the row is already durably gone, so the owner *is* reaped and there is
nothing to retry, and `repair_abandoned_uploads` has no per-candidate `try`, so one bad key would
abandon every later owner in the pass. `CategorizedError` is caught specifically — the category the
store wraps `BotoCoreError`/`ClientError` in, matching `_cleanup_chunks_and_manifest`'s precedent —
so a programming error still crashes and `CancelledError` still propagates.

Swallowing it *entirely*, however, would silently degrade an existing signal, so the counts travel
up as `ReapOutcome.undeleted` and `repair_abandoned_uploads` raises once, **after** the loop.
`_run_repair_plan` puts only a repair that raises into `failures`, and `failures` is the sole input
to the ADR-0190 group-E error counter — so without that raise, a store rejecting every delete would
add N to `kdive.reconciler.repairs` and zero to `kdive.errors`, making the one condition that leaks
bytes permanently the healthiest-looking pass on the dashboard, and leaving #1556's backlog
unmeasurable. The pass forfeits its reaped count to say so, which is the right way round: the count
is a gauge, the error is the alert, and the rows are durably deleted either way. Raising after the
loop rather than inside it is what keeps "one bad key costs no other owner its reap" true at the
same time.

### 4. A whole owner's sweep failing stops the pass claiming more candidates

Per-key tolerance alone would be unbounded in the one direction that matters. The candidate select
carries no `LIMIT`, and a *systemic* delete fault — a bucket policy granting `s3:PutObject` and
`ListBucket` but not `s3:DeleteObject`, an endpoint or proxy rejecting DELETE — fails every key of
every owner while LIST keeps succeeding. Tolerating that per key would walk the entire past-deadline
backlog in one pass, commit an irreversible row delete for each, orphan all of their bytes where
nothing reclaims them (#1556), and repeat every 30 seconds. Row-first is what makes a *single* leak
acceptable; nothing in it bounds how many single leaks one pass produces.

So `ReapOutcome.store_refused_everything` — every key of a non-empty sweep failed — stops the loop
claiming further candidates, and the end-of-pass raise names how many were left unclaimed. One bad
key is still just a bad key: a partial failure sweeps the remaining keys and the remaining owners,
exactly as before. This also makes the delete side consistent with the list side: a failing
`store.list_prefix` already ends the pass — phase 1 catches nothing, and that abort is *free*,
because it precedes the row-delete commit, so the transaction rolls back with nothing deleted — and
a store that lists but refuses to delete had been getting the opposite treatment for no stated
reason. The cap is one owner's leak per pass, and the unclaimed candidates are re-read unchanged 30
seconds later with their rows and bytes intact.

Those logs only cover the failure modes phase 2 can *observe*. The abort modes this ADR's Context
names as motivating — cancellation at shutdown, a lost connection, a process kill — unwind past
them, leaving no record that the claim happened at all. `_claim_abandoned_prefix` therefore logs
the prefix and the doomed key count at INFO once its transaction commits — before the sweep begins. What that buys is the *timestamp* and
the *count*: it is deliberately not claimed to rescue an otherwise-lost handle, because the prefix
is `owner_prefix(_TENANT, owner_kind, owner_id)` from the single mint site and so stays derivable
from the owner row, which outlives the reap (§Consequences records this, since #1556 needs it).

## Consequences

Two residuals are disclosed and neither is fixed here.

**The leak is real and unswept.** A phase-2 failure — or a crash between the commit and the last
delete — leaves objects under `local/<kind>/<id>/` with no manifest row and no `artifacts` row.
Nothing in this tree will ever reclaim them: `gc_expired_build_artifacts` enumerates `artifacts`
rows, the `images/` orphan scan is scoped to a different prefix, and there is no lifecycle rule.
The claim that #768's reaper covers this is false and is not relied on. Filed as **#1556** for an
upload-prefix orphan sweep (or a bucket lifecycle rule). Row-first converts a correctness bug into
a storage-cost bug that is invisible until someone looks at the bucket — accepted, because a
dangling `kernel_ref` on a `succeeded` Run is not recoverable by any later sweep and leaked bytes
are. The leak is **enumerable**, which is what makes that trade cheap to unwind: the prefix is
`owner_prefix(_TENANT, owner_kind, owner_id)` from the single mint site, so #1556 can walk the
`runs`/`investigations` tables, derive each prefix, list it, and subtract the `artifacts` rows —
the same shape as the existing `images/` scan — rather than scraping logs or reaching for a
bucket-wide lifecycle rule. §Decision 4 caps how much one degraded pass can add to that backlog.

**Phase 2 no longer serializes against the writers that can commit a row at a doomed key.** The
sweep deletes the phase-1 key list unconditionally, re-reading nothing, and it holds no owner lock
while it does — so any writer that can put an object at one of those keys, or commit an `artifacts`
row for one, wins a race the old order made impossible by holding the `RUN` lock across the per-key
check and its delete. Three writers qualify (§Decision 2 names them at the site):

- A **re-mint**. Upload keys are owner-addressed, so a re-minted window reuses the same key names.
  The trigger is one re-minted PUT landing on a key before the sweep reaches that key's
  `delete_object`; the finalize may commit arbitrarily later and still be corrupted, because
  `_require_unreaped_window` compares the *new* window's deadline against the one that finalize
  itself validated, and they match. It is not exotic — a re-mint is the *documented recovery* from
  a reap (ADR-0448), so this sits on the recovery path, not off it.
- **`control.capture_traffic`.** `local/runs/<id>/` is the owner prefix, so its `pcap-<job_id>`
  object is doomed by phase 1 whenever it exists rowless — which happens when an earlier attempt's
  PUT survived a rolled-back transaction. The retry re-PUTs the same key and commits the row; if
  that lands during phase 2, the row outlives its object. **Newly** exposed by this change.
- **The vmcore rows.** The provider PUTs `local/runs/<id>/<name>` outside any lock, well before
  `finalize_capture` inserts the rows. This exposure predates this change (the object was already
  rowless outside the lock) and is not widened by it, but the barrier claim would have denied it,
  so it is disclosed here rather than left implied.

This also **puts the investigations lane in scope** for the re-mint case, which the Context excused
for the *original* defect and cannot excuse here: `complete_rootfs_upload`'s single-transaction
`INVESTIGATION`-locked finalize serialised against a reap only while the reap held that lock, and
phase 2 does not.

In practice the sweep is a handful of `delete_object` calls and finishes well inside any of these
windows, so reaching it needs a stalled or degraded store — which is exactly the condition this
change exists for.

All three are filed as **#1557**, with the options costed there rather than guessed at here — one
issue rather than three, because the per-key re-check that closes any of them closes all of them.
None is free: re-reading `upload_manifests` once before the sweep is one indexed query but sits
*before* the delete loop, which is the exposed interval, and does nothing for the two non-manifest
writers; a per-key re-check (`repair_leaked_images`' precedent) shrinks the window to one
check→delete gap for all three but returns a query per key and a database connection to the phase
#1554 wants free of both; a store-mtime grace (also `repair_leaked_images`' precedent) closes them
outright but needs a prefix-parameterised sibling of `list_image_objects` and permanently leaks any
object whose presigned PUT began before the deadline and completed after it — a routine path, not a
rare one, traded against a rare corruption. Narrowing the doomed set to the manifest's own declared
entries and their `chunk_key` derivatives would exclude the pcap and vmcore keys entirely, but it
also retires the prefix sweep's stray-object backstop (ADR-0048 §6, ADR-0104 §7), which is a
decision for #1557, not a side effect of a reorder. An identity-conditioned delete does not close
anything: `Last-Modified` has one-second granularity and a byte-identical re-upload carries the same
etag.

`repair_abandoned_uploads` still has no per-candidate `try`, so **any** fault during one owner's
phase 1 ends the pass — `CategorizedError` included, and a failing `store.list_prefix` is the most
likely one in the store outage this change is built for. That is unchanged behaviour, it is bounded
(`_run_repair_plan` isolates each repair, so only this repair's pass is lost), and it costs nothing:
phase 1 aborts before the row delete commits, so the next 30-second pass re-reads the same
candidates with no objects deleted.

The two-phase split is also the shape #1554's concurrent sweep needs: a short locked transaction
that decides, and a phase touching only the object store, fannable out without holding a lock. It
is **not** yet free of a pooled connection, and #1554 should not read it as such: the connection
left `_sweep_uncommitted_objects`' signature, not the call stack, and `_run_repair_plan` keeps
`pool.connection()` checked out around the whole `repair_abandoned_uploads` call — so on a degraded
store one pooled connection now sits idle for the length of the sweep, and a fan-out would pin it
for the length of the widest branch. Making that real means restructuring the driver, which is
#1554's to do.

No schema, no migration, no config setting, no MCP or RBAC surface, and no change to either
finalize path. No **new** metric — but the ADR-0190 group-E error counter's meaning for this repair
is preserved deliberately rather than by accident, via §Decision 3's end-of-pass raise; without it
this change would have silently retired that signal. Not an AI surface.
