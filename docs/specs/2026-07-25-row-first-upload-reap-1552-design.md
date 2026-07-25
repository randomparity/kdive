# Commit the upload manifest-row delete before sweeping its objects (#1552)

- **Issue:** [#1552](https://github.com/randomparity/kdive/issues/1552)
- **ADR:** [ADR-0453](../adr/0453-row-first-upload-reap.md)
- **Status:** implemented

## Problem

`reap_one_owner` (`reconciler/cleanup/uploads.py`) deletes a past-deadline upload window's S3
objects **inside** the transaction that deletes its `upload_manifests` row. The object deletes are
immediate and irreversible; the row delete commits only at transaction exit. Anything that aborts
that transaction after the loop has started — a `CategorizedError` out of `store.delete`, a lost
connection, task cancellation at shutdown — rolls the **row** back with the **bytes** already gone.

The delete is a per-object loop, one `delete_object` call per key, so a partial failure is the
normal shape of an S3 outage here, not an exotic edge.

What remains is an `upload_manifests` row that is byte-identical to the window it was before the
reap started — same `prefix`, same `deadline` — over objects that no longer exist.

### The lane that is actually exposed

The **runs single-PUT** finalize. `_require_unreaped_window` (`services/runs/complete_build.py`,
ADR-0448 §2) re-reads the manifest under the `RUN` lock and compares the *identity* of the window
— its deadline stamp. That defeats a **committed** reap (the row is gone) and a **re-mint** (the
deadline is re-stamped). A **rolled-back** reap re-stamps nothing: the restored row carries the
original deadline, so it is exactly the window finalize validated. The guard passes, and
`complete_build` registers `artifacts` rows carrying the deleted objects' etags and marks the Run
`succeeded` with a dangling `kernel_ref`. Reaching it requires a finalize that straddles the
deadline while a reap fails partway — narrow, but reachable.

### Two claims in the issue body that do not hold

1. *"A row deleted with objects present is a leak the #768 expiry reaper already handles."* It is
   not. `gc_expired_build_artifacts` (`reconciler/cleanup/gc.py`) is **row-driven** over
   `artifacts`, and the reaper by construction only ever deletes objects that have **no**
   `artifacts` row — so a row-first orphan is structurally invisible to it. The only prefix-driven
   orphan scan covers the `images/` prefix (`reconciler/cleanup/images.py`, `list_image_objects`),
   not the upload prefix `local/runs/<id>/`, and there is no S3 lifecycle rule anywhere in `src/`.
   With the manifest row gone, nothing rediscovers the prefix. The asymmetry still favours
   row-first, but it is a trade against an **unswept** leak, not a swept one.

2. *"Affects the investigations lane as well as the runs lane."* Both lanes do share this one
   function (`_LOCK_SCOPES` maps both owner kinds into it — there is no duplicate reaper), but the
   investigations lane is already safe against this defect.
   `investigations.complete_rootfs_upload` does its HEAD, its `artifacts` row write, and its
   `delete_manifest` inside **one** transaction under the same `LockScope.INVESTIGATION` the reaper
   takes, so it cannot observe a rolled-back reap: it either runs entirely before the reap
   transaction or entirely after it, and after a rollback the window it sees is intact. Fixing the
   shared function of course covers it too; it is not the motivation.

## Requirements

1. The `upload_manifests` row delete commits **before** any object under its prefix is deleted.
2. The committed-object exemption is unchanged: an object holding an `artifacts` row is never
   deleted, for either owner kind, in any owner state.
3. A failure partway through the object sweep leaves **no** `upload_manifests` row for that owner —
   the reap is durable regardless of what the object store does.
4. A failed key does not abort the sweep of the remaining keys, and does not abort the reap of
   other owners in the same pass.
5. Object deletion holds neither the owner advisory lock nor a database transaction.
6. The locked re-read still declines a manifest whose deadline was renewed since the candidate
   select, and an unknown owner kind still fails loud rather than locking under a guessed scope.

## Design

### Two phases, split on the commit

`reap_one_owner` becomes a driver over two helpers.

**Phase 1 — `_claim_abandoned_prefix`** runs in the existing transaction under the existing
per-owner advisory lock and does every decision that needs the database:

1. re-read the past-deadline manifest row (returns `None` — declined — if it is gone or renewed);
2. `list_prefix` the window's prefix;
3. subtract the keys that hold a committed `artifacts` row;
4. `DELETE` the manifest row.

It returns the surviving keys — the *doomed* set — and the transaction commits on exit.

**Phase 2 — `_sweep_uncommitted_objects`** takes only the store and that key list. No connection,
no transaction, no lock. It deletes each key, catching `CategorizedError` per key so one failure
does not strand the rest.

### Why the committed-object verdict cannot go stale across the split

The concern with computing the exemption in phase 1 and acting on it in phase 2 is that a key
could acquire an `artifacts` row in between, after which deleting it would be exactly the
corruption this issue is about. It cannot, for this window: the only writer that inserts an
`artifacts` row against these keys is the owner's finalize, and both finalizes require the
manifest row that phase 1 just deleted. `_require_unreaped_window` rejects with
`no_upload_manifest`; `complete_rootfs_upload` rejects for a missing manifest. **The committed row
delete is itself the barrier.** That is the property that makes row-first correct rather than
merely differently-ordered, and it is only available in this direction — object-first has no
barrier at all.

### One set-valued query instead of a query per key

The per-key `SELECT 1 FROM artifacts WHERE object_key = %s` becomes a single
`WHERE object_key = ANY(%s)`. Same verdict, one round trip, and it shortens the locked phase —
which matters now that phase 1 is the only part holding the lock.

### Failure handling in phase 2

A failed `store.delete` is logged at WARNING with the key, and the loop continues. After the loop,
a non-zero failure count is logged at ERROR naming the owner and the counts, because those bytes
are now unreferenced and (per Problem, claim 1) nothing will rediscover them — the log is the only
signal that exists. Raising instead would be worse on every axis: the row is already durably gone,
so the owner *is* reaped and there is nothing to retry; and `repair_abandoned_uploads` has no
per-candidate `try`, so a raise would also abandon every later owner in the pass over one bad key.
`CategorizedError` is caught specifically — the category the store wraps `BotoCoreError`/
`ClientError` in — matching `_cleanup_chunks_and_manifest`'s precedent, so a programming error
still crashes and `CancelledError` (a `BaseException`) still propagates.

### Shape for #1554

#1554 lands a concurrent sweep in this function. The split it needs is the one drawn here: a short
locked transaction that decides, and a phase that touches only the object store — parallelizable
without holding a lock or a pooled connection across the fan-out.

## Alternatives considered

- **Keep object-first, wrap the loop so a failure cannot abort the transaction.** Suppressing the
  error still leaves the row committed-deleted on the success path and does nothing about a lost
  connection or cancellation, which abort the transaction without any exception the loop can see.
- **Two-phase commit / a `reaping` state column on `upload_manifests`.** Would make the
  intermediate state recoverable, and is the only shape that leaks nothing. It needs a migration
  and a resume path; #1552 does not justify either, and the disclosed leak is the price.
- **Delete objects conditionally on a captured identity (etag / last-modified).** Narrows the
  re-mint residual below, at the cost of a HEAD per key and a widened `UploadStore` port, and does
  not close it (S3 `Last-Modified` has one-second granularity, and a byte-identical re-upload has
  the same etag). Not taken; the residual is disclosed and filed instead.

## Residuals

Both are recorded in ADR-0453 §Consequences and filed as follow-up issues.

1. **The unswept leak.** A phase-2 failure leaks objects under `local/<kind>/<id>/` with no
   manifest row and no `artifacts` row. Nothing in the tree sweeps that prefix.
2. **The re-mint window.** Phase 2 no longer holds the owner lock, so a re-mint can now interleave
   between the row-delete commit and the object deletes. Upload keys are owner-addressed, so a
   re-minted window reuses the same key names, and the deferred delete would remove the new
   window's bytes. It requires a full create-upload → PUT → finalize cycle to complete inside the
   time it takes to issue N `delete_object` calls, against the pre-existing defect's requirement
   that a finalize merely straddle the deadline.

## Test plan

New tests in `tests/reconciler/test_upload_reaper.py`, all against real Postgres:

- **Mid-sweep failure, row end state.** A store whose `delete` raises `CategorizedError` on the
  second of three keys: the manifest row is gone, the reap counts as reaped, the first and third
  keys are deleted (the failure does not strand the rest), and the second is not.
- **Mid-sweep failure does not abandon later owners.** Two past-deadline owners, the first one's
  sweep failing: both rows are gone and the tally is 2.
- **Ordering is observable.** A store whose `delete` asserts, from inside the callback, that the
  manifest row is already absent — proving the commit precedes the first byte deleted rather than
  just asserting the end state.
- **Committed-object exemption under the set-valued query**, including the mixed case (one
  committed key, one stray) and the all-committed case (empty doomed set, no `delete` call).
- Existing reaper tests carry forward unchanged; they already pin the exemption, the renewed-window
  decline, and the fail-loud unknown owner kind.
