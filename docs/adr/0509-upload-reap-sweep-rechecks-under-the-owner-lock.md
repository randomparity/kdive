# ADR 0509 — The upload reap's object sweep re-checks each key under the owner lock, and declines the store-mtime grace

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** KDIVE maintainers

## Context

[ADR-0453](0453-row-first-upload-reap.md) §1 made the upload reap row-first: `_claim_abandoned_prefix`
commits the `upload_manifests` row delete under the owner's advisory lock, and
`_sweep_uncommitted_objects` then deletes the key list that claim produced. The second phase held
**neither** the lock nor a connection, and re-read nothing — its own docstring said so, and named
the cost: "anything that lengthens this phase widens #1557".

The cost is that the phase-1 key list stops being true the instant phase 1 commits. Upload keys are
owner-addressed (`local/runs/<id>/<name>`), and the prefix phase 1 lists is the **owner** prefix,
not an upload-only namespace. Three writers reach it, all verified in the tree:

1. **A re-mint.** ADR-0448 makes a re-mint the documented recovery from a reap, so an agent whose
   finalize was rejected with `no_upload_manifest` re-mints at exactly the moment the sweep may
   still be running. The new window reuses the same key names, and phase 2 deletes them.
2. **`control.capture_traffic`.** `_store_capture` writes `local/runs/<id>/pcap-<job>` with its PUT
   and its `artifacts` insert in one `LockScope.RUN` transaction. A rolled-back earlier attempt
   leaves the object rowless; phase 1 dooms it; the retry re-PUTs and commits its row; phase 2 then
   deletes an object that now **has** an `artifacts` row.
3. **The vmcore pair.** `providers/local_libvirt/retrieve.py` PUTs outside any lock and
   `finalize_capture` inserts the rows later. [ADR-0502](0502-a-write-lease-closes-the-orphan-sweep-delete-race.md)
   gave that lane a write lease, but the lease is not consulted anywhere on the reaper path.

Both owner lanes are exposed. ADR-0453 recorded that `investigations` was never in scope for the
defect #1552 removed, because `complete_rootfs_upload` does its HEAD, its `artifacts` write and its
`delete_manifest` in one transaction under the `INVESTIGATION` lock the reaper also took. That
argument does not survive the split: phase 2 no longer holds that lock.

Four ADRs declined to fix this ([0455](0455-upload-prefix-orphan-sweep.md) §3,
[0497](0497-finalize-verifies-its-object-before-committing-rows.md),
[0498](0498-page-the-upload-orphan-sweep.md), [0502](0502-a-write-lease-closes-the-orphan-sweep-delete-race.md)),
each for the same reason: the mechanism that closes it was not built yet. ADR-0502 built it. Its
`_delete_if_still_reclaimable` re-decides one key and deletes it **inside one transaction holding
the owner's advisory lock**, and that pairing — `hold_write_lease` mints under the same lock — is
what turns a set of fences into a closure rather than a fourth mitigation. This ADR applies the same
shape to the reaper's phase 2, which is the last deleter that does not use it.

Two mechanisms are ruled out before the decision, and neither should be re-derived. ADR-0497 §1
measured `If-Match` on `DeleteObject` against both pinned MinIO releases and found it inert — the
header reaches the wire, the call returns success, and the object is destroyed on every arm — so
this cannot be specified around a conditional delete. And ADR-0502 §Considered-&-rejected records
why `upload_manifests` itself cannot be the writer's fence.

## Decision

**1. Phase 2 becomes a per-key re-check and delete under the owner's advisory lock.**
For each doomed key, `_sweep_uncommitted_objects` opens one top-level transaction, takes
`lock_scope_for(owner_kind)`/`owner_id` **non-blocking**, re-evaluates the owner fences against
committed state, and calls `store.delete` inside that transaction. `hold_write_lease` mints under
that same lock, and `capture_traffic` holds it across its whole PUT-plus-insert, so a writer either
precedes the re-check — which then sees its row or its lease and declines — or waits until the
delete has already happened, in which case the write is not the one being deleted.

**2. The fences are the orphan sweep's fences, shared as one SQL fragment.**
A new module `reconciler/cleanup/upload_fences.py` holds `_OWNER_FENCE_SQL` — no committed
`artifacts` row reaches the key, the owner holds no `upload_manifests` row at all, the owner holds
no write lease with a live holder — and both callers embed it: `reclaimable_upload_keys` for the
orphan sweep's page classify and per-key re-check, `owner_key_is_fenced` for the reaper's. The two
passes cannot drift into disagreeing about what is safe to delete, which is the property ADR-0455 §2
already claimed for the sweep's own two call sites. `UploadOrphanCandidate` moves with them; nothing
about it changes.

**3. The reaper does not apply the store-mtime grace, and therefore issues no HEAD.**
`reclaimable_upload_keys` also holds a candidate behind `orphan_grace + upload_ttl` measured on its
store mtime. The reaper declines that term. Its candidate set is not "every rowless object under a
root" but "the objects of one window this pass has just proved past its deadline and whose row it
has just deleted under the lock", so the grace protects nothing the lock and the three row fences do
not already protect — while costing the reap of every chunk of every abandoned window a full
`orphan_grace` (86 400 s by default), which would leave the reaper deleting nothing it was built to
delete and double the storage residency of every abandoned upload. Declining the mtime term is also
what keeps the store port unwidened: with no grace there is nothing for a HEAD or an
mtime-bearing listing to feed, so `UploadStore` keeps its two methods.

**4. A declined key is not a failure.**
Three outcomes per key, counted separately: deleted, declined (the owner lock was held, or a fence
now protects the key), failed (the store refused the delete). Only `failed` feeds ADR-0453 §3's
end-of-pass raise and §4's brake on claiming further candidates; a decline is the guard working. A
declined key is never revisited by the reaper — its manifest row is already gone — and it does not
need to be: `repair_leaked_upload_objects` (ADR-0455) exists precisely to drain rowless objects under
these roots, and a key the reaper declined either belongs to a live window (and is not an orphan) or
becomes that sweep's candidate once past its own grace.

## Consequences

**A connection is back in phase 2, and #1554 must honour that.** #1554 fans this sweep out, and
ADR-0453 said the two decisions had to be settled together. This one is first, so it decides:

- Each concurrent sweep worker needs **its own pooled connection**, transaction-free at the point it
  starts a key. A shared connection turns each `conn.transaction()` into a `SAVEPOINT`, which
  releases no `pg_advisory_xact_lock` — the owner lock would then be held for the whole fan-out
  rather than for one delete, which is the multi-GiB lock hold ADR-0244 forbids. This is asserted,
  not documented: phase 2 calls `require_top_level_transaction` per key.
- **Fan out across owners, not across one owner's keys.** Every key of one owner contends on the
  *same* advisory lock, so within-owner concurrency serialises on that lock and buys nothing while
  multiplying lock churn; different owners hash to different lock keys and are genuinely parallel.
- The concurrency ceiling is therefore the connection pool (ten slots), of which `_run_repair_plan`
  already holds one for the pass — not the store's request parallelism.
- The pass-level aggregation (§3's raise, §4's brake) must sum across workers, and `declined` must
  stay out of both.

**Cost.** One lock acquisition, one statement and one round trip per doomed key. That is strictly
less than `_claim_abandoned_prefix`, which already holds `LockScope.RUN` across a whole paginating
`list_prefix`, and it is the same per-key cost `_delete_if_still_reclaimable` has carried since
ADR-0502. In the other direction: an unresponsive `delete` now holds one owner's lock for botocore's
retry budget, and every foreground operation on that Run waits — the cost ADR-0502 §Consequences
already disclosed for the orphan sweep, now also paid here.

**This completes ADR-0502's guarantee.** A `capture_vmcore` write holding an ADR-0502 lease was safe
from `repair_leaked_upload_objects` and not from `repair_abandoned_uploads`. Both deleters now
consult the lease under the lease's own lock, so "a declared write is never destroyed" holds against
every deleter in the tree rather than one of two.

**Residual: a writer that declares nothing.** A writer that PUTs under an owner prefix while holding
no owner lock, no write lease and no committed `artifacts` row is still invisible to this guard, and
the mtime grace this ADR declines would have given it a margin. No such writer exists today — the
vmcore lane leases, `capture_traffic` locks, a re-mint writes a manifest row — and the mechanism for
any future one is `hold_write_lease`, which is one call before the first write. The grace was never a
closure for that writer either, only a wider window; the declaration is.

**A behaviour test is inverted.** `test_a_key_that_gains_an_artifacts_row_after_the_claim_is_still_deleted`
pinned the unguarded behaviour and was written to fail the day this closed. It is replaced by its
opposite, under a name that says so.

## Considered & rejected

**A store-mtime grace on the doomed keys, from phase 1's listing.** Free — S3's `ListObjectsV2`
already returns `LastModified` and `list_prefix` discards it — but unsound. A listed mtime is stale
by the time phase 2 deletes, and staleness runs the wrong way: a key re-PUT after the listing still
carries its *old* mtime, so the grace waves through exactly the object it exists to protect. This is
the reasoning ADR-0496 already applied to the orphan sweep's re-read.

**A store-mtime grace from a fresh HEAD per key.** Sound, and it is what the orphan sweep does. It
costs a network round trip per doomed key on top of the query, and it buys a margin only for a writer
that declares nothing — while, at any grace large enough to matter, deferring the reap of every
recently-uploaded chunk to a sweep whose own threshold is `orphan_grace + upload_ttl`. A grace small
enough not to do that is a number nothing in the tree bounds, which is the same objection ADR-0502
raised against giving the write lease a deadline of its own.

**One lock held across the whole owner's sweep.** Fewer acquisitions, and it would let the re-check
be a single set-valued query. Rejected: it holds the owner's lock across N `delete_object` calls, so
one stalled store pins every foreground operation on that Run for N × botocore's retry budget. The
per-key shape bounds the hold to one delete, which is the bound ADR-0244 asks for and ADR-0502
already ships.

**Blocking on the owner lock instead of `try`.** A reconciler pass has no deadline, so waiting puts
it behind whatever the holder is doing — `capture_traffic` holds `LockScope.RUN` across a whole
`put_artifact` — and ahead of allocation expiry and orphaned-System repair. ADR-0455 §5 and §6 exist
to prevent that starvation. Skipping costs a delayed reclaim the orphan sweep already drains.

**Re-reading `upload_manifests` once before the delete loop, rather than per key.** One indexed
query, no per-key cost. Rejected on two counts: the guard would sit *before* the loop, which is the
exposed interval, and arms 2 and 3 involve no manifest row at all, so it closes only the re-mint arm.
ADR-0453 §Consequences costed it and reached the same verdict.

**Narrowing the doomed set to the manifest's declared entries plus their `chunk_key` derivatives.**
Excludes the pcap and vmcore keys outright and shrinks the re-mint arm. Rejected because it retires
the prefix sweep's stray-object backstop (ADR-0048 §6, ADR-0104 §7) — the reap would stop collecting
anything the agent PUT under the prefix but did not declare, which is the failure mode the prefix
listing exists for. That is a separate decision with its own consequences, not a side effect of a
race fix.

**An identity-conditioned delete.** ADR-0497 §1 measured `If-Match` on `DeleteObject` as inert on
both pinned MinIO releases, and `botocore` models the parameter, so a guard built on it passes every
S3 stub and destroys the object in production. `Last-Modified` has one-second granularity and a
byte-identical re-upload carries the same ETag, so neither is an identity either.
