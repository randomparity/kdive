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
   not. `gc_expired_build_artifacts` (`reconciler/cleanup/artifact_retention.py`) is **row-driven**
   over
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
4. A failed object **delete** does not abort the sweep of the remaining keys, and does not abort
   the reap of other owners in the same pass. It is still *reported*, once the pass is complete, so
   the repair's existing group-E error signal survives the change. This does not extend to phase 1:
   a failing `list_prefix` ends the pass immediately, deliberately — see Design, "Failure handling".
5. Object deletion holds neither the owner advisory lock nor a database transaction.
6. The window's prefix and doomed key count are recorded before the first object is deleted, so an
   abort that skips the sweep's own reporting still leaves a record of when the claim happened and
   how much it doomed.
7. The locked re-read still declines a manifest whose deadline was renewed since the candidate
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

**Phase 2 — `_sweep_uncommitted_objects`** takes only the store and that key list. No connection
argument, no transaction, no lock. It deletes each key, catching `CategorizedError` per key so one
failure does not strand the rest, and returns how many it could not delete. (`_run_repair_plan`
still keeps a pooled connection checked out around the whole pass, so the phase is connection-free
in its signature, not in the call stack — see "Shape for #1554".)

### How far the committed-object verdict holds across the split

The concern with computing the exemption in phase 1 and acting on it in phase 2 is that a key could
acquire an `artifacts` row in between, after which deleting it would be exactly the corruption this
issue is about. **The committed row delete is itself the barrier** against the reaped window's own
finalizes: both require the manifest row phase 1 just deleted, so neither can run for it —
`_require_unreaped_window` rejects with `no_upload_manifest`, `complete_rootfs_upload` rejects for a
missing manifest. That is the property that makes row-first correct rather than merely
differently-ordered, and it is only available in this direction: object-first has no barrier at all,
because the state authorizing the deletes is not durable until the row delete commits.

It does **not** extend to every writer that can reach these keys, and the design does not pretend
otherwise. Phase 2 holds no owner lock, where the old order held the `RUN`/`INVESTIGATION` lock
across the per-key check *and* its delete. Three writers get in:

- a **re-mint**, which creates a new manifest row and so a new window over the same
  owner-addressed key names;
- **`control.capture_traffic`**, whose `pcap-<job_id>` object lives under this same *owner* prefix
  (`local/runs/<id>/`, from `owner_prefix` — the manifest prefix is not an upload-only namespace)
  and is doomed by phase 1 whenever an earlier attempt's PUT survived a rolled-back transaction; a
  retry re-PUTs the key and commits the row. Newly exposed;
- **the vmcore rows**, whose objects the provider PUTs outside any lock long before
  `finalize_capture` inserts them. Pre-existing, not widened, but disclosed because an unqualified
  barrier claim would have denied it.

All three are residual 2 (#1557), and `test_a_key_that_gains_an_artifacts_row_after_the_claim_is_
still_deleted` pins the behaviour so the residual has a reproducer.

### One set-valued query instead of a query per key

The per-key `SELECT 1 FROM artifacts WHERE object_key = %s` becomes a single
`WHERE object_key = ANY(%s)`. Same verdict, one round trip, and it shortens the locked phase —
which matters now that phase 1 is the only part holding the lock.

### Failure handling

A failed `store.delete` is logged at WARNING with the key, and the loop continues. After the loop,
a non-zero failure count is logged at ERROR naming the owner and the counts. Raising *there* would
be worse on every axis: the row is already durably gone, so the owner *is* reaped and there is
nothing to retry; and `repair_abandoned_uploads` has no per-candidate `try`, so a raise would also
abandon every later owner in the pass over one bad key. `CategorizedError` is caught specifically —
the category the store wraps `BotoCoreError`/`ClientError` in — matching
`_cleanup_chunks_and_manifest`'s precedent, so a programming error still crashes and
`CancelledError` (a `BaseException`) still propagates.

Swallowing it *entirely* would degrade an existing signal, so the counts travel up as
`ReapOutcome.undeleted` and `repair_abandoned_uploads` raises once, **after** every candidate has
been reaped. `_run_repair_plan` puts only a raising repair into `failures`, and `failures` is the
sole input to the ADR-0190 group-E error counter — so without the end-of-pass raise, a store
rejecting every delete would add N to `kdive.reconciler.repairs` and zero to `kdive.errors`, making
the one condition that leaks bytes permanently the healthiest-looking pass on the dashboard, and
leaving #1556's backlog unmeasurable. The pass forfeits its reaped count in exchange: the count is
a gauge, the error is the alert, and the rows are durably deleted either way. Raising after the
loop rather than inside it keeps requirement 4 true at the same time.

Those logs only cover what phase 2 can observe. The abort modes in Problem above — cancellation at
shutdown, a lost connection, a process kill — unwind past them, leaving no record that the claim
happened at all. `_claim_abandoned_prefix` therefore logs the prefix and the doomed key count at
INFO once its transaction commits, before the sweep begins (requirement 6). What that buys is the *timestamp* and the *count*, and
deliberately not a rescued handle: the prefix is `owner_prefix(_TENANT, owner_kind, owner_id)` from
the single mint site (`mcp/tools/catalog/artifacts/uploads.py`, the only construction of
`UploadManifestReplaceRequest` in `src/`), so it stays derivable from the owner row, which outlives
the reap. Overstating that would mislead #1556, which needs exactly this fact to scope its sweep.

### Bounding a systemic delete fault

Per-key tolerance alone is unbounded in the one direction that matters. The candidate select has no
`LIMIT`, and a systemic delete fault — a bucket policy granting `s3:PutObject` and `ListBucket` but
not `s3:DeleteObject`, an endpoint rejecting DELETE — fails every key of every owner while LIST
keeps succeeding, so the pass would walk the whole past-deadline backlog, commit an irreversible row
delete for each, orphan all their bytes, and repeat every 30 seconds. Row-first makes a *single*
leak acceptable; it says nothing about how many one pass produces.

`ReapOutcome.store_refused_everything` — every key of a non-empty sweep failed — therefore stops the
loop claiming further candidates, and the end-of-pass raise names how many were left. A partial
failure is still just a bad key and still sweeps the remaining keys and owners (requirement 4).

This also makes the two phases consistent. `store.list_prefix` raises `CategorizedError` on the same
faults `store.delete` does — in a store outage a failing LIST is the likelier of the two — and phase
1 catches nothing, so a listing failure ends the pass and drops the later candidates. That abort is
*free*: it precedes the row-delete commit, so the transaction rolls back with nothing deleted and
the next pass retries the same candidates unchanged. `_run_repair_plan` isolates each repair, so the
blast radius is this one repair's pass either way. Tolerance is bought only where a retry is
impossible, and capped where it is irreversible.

### Shape for #1554

#1554 lands a concurrent sweep in this function. The split it needs is the one drawn here: a short
locked transaction that decides, and a phase that touches only the object store — parallelizable
without holding a lock.

Two caveats it must not inherit as guarantees. First, the phase is **not** free of a pooled
connection: `_run_repair_plan` holds `pool.connection()` around the whole `repair_abandoned_uploads`
call, so the connection left the signature, not the call stack, and on a degraded store it now sits
idle for the length of the sweep. A fan-out pins it for the widest branch; making it genuinely
connection-free means restructuring the driver. Second, phase 2 deletes its key list
unconditionally, so anything that lengthens or fans out that phase widens #1557.
`_sweep_uncommitted_objects`' docstring says both at the site.

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
- **Re-read `upload_manifests` for the owner once before the sweep, abandoning it if a row
  exists.** One indexed query, no port change. Not taken because it buys almost nothing: the guard
  would sit *before* the delete loop, and the delete loop is the exposed interval. A guard that
  narrows the window by the time it takes to run one query, while implying the hole is closed, is
  worse than an honest disclosure.
- **Re-check each key's protection immediately before its delete**, the precedent
  `repair_leaked_images` already sets in this package ("a row that landed between the listing and
  the delete protects its object"). This genuinely shrinks the re-mint residual to a single
  check→delete gap per key. Not taken here: it returns a query per key and a live database
  connection to the phase #1554 wants free of both, so the two decisions should be costed together
  — which is what #1557 says.
- **Gate the deletes on a store-mtime grace**, `repair_leaked_images`' other guard. It closes the
  re-mint residual outright, since a re-minted object is newer than the reaped window's deadline.
  Not taken: it needs a prefix-parameterised sibling of `list_image_objects` (the existing one
  hardcodes `images/`), and it would permanently leak any object whose presigned PUT began before
  the deadline and completed after it — a routine path, since the sweep runs within 30s of the
  deadline — trading a rare corruption for a routine leak that, per residual 1, nothing collects.

## Residuals

Both are recorded in ADR-0453 §Consequences and filed.

1. **The unswept leak — [#1556](https://github.com/randomparity/kdive/issues/1556).** A phase-2
   failure, or a crash between the commit and the last delete, leaks objects under
   `local/<kind>/<id>/` with no manifest row and no `artifacts` row. Nothing in the tree sweeps
   that prefix. It is at least **enumerable**: the prefix is a pure function of the owner, which
   outlives the reap, so #1556 can walk the owner tables and derive it — the same shape as the
   existing `images/` scan. "Bounding a systemic delete fault" above caps how fast one degraded
   pass can grow the backlog.
2. **The unlocked sweep's races — [#1557](https://github.com/randomparity/kdive/issues/1557).**
   Phase 2 holds no owner lock and re-reads nothing, so any writer that can put an object at a
   doomed key, or commit an `artifacts` row for one, wins a race the old order made impossible.
   Three do: a **re-mint** (owner-addressed keys, so the new window reuses the names — and this
   puts the **investigations** lane in scope too, since Problem claim 2 holds only for the original
   defect); **`control.capture_traffic`**'s orphan-then-retry pcap, newly exposed; and the
   **vmcore** rows, pre-existing and not widened. See "How far the committed-object verdict holds"
   for each. A re-mint is the documented recovery from a reap, so that arm sits on the recovery
   path; reaching any of them needs a sweep slow enough to be raced.

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
- **The leak is reported.** A store whose every delete fails: one WARNING per key naming the key,
  and exactly one ERROR summary naming the owner and the counts — with a companion test asserting a
  clean sweep emits neither, so the summary is conditional rather than unconditional.
- **The prefix is on the record before the first delete**, asserted by position in the log
  sequence rather than by mere presence.
- **A failing `list_prefix` aborts the pass and deletes nothing**, pinning the deliberate asymmetry
  above: no delete is attempted and the manifest row survives intact for the retry.
- **A totally failed sweep reports as a failed pass**, with an `INFRASTRUCTURE_FAILURE`
  `CategorizedError` naming the object and owner counts — and a companion test that a clean sweep
  returns the reaped count and does not raise, so the signal is conditional.
- **A wholly refused sweep stops the pass**: three past-deadline owners, every delete failing —
  exactly one row goes, two survive for the next pass, and only one owner's keys are attempted.
  Written order-independently, since the candidate select has no `ORDER BY`.
- **A key that gains an `artifacts` row after the claim is still deleted**, pinning residual 2 as
  known behaviour with a reproducer rather than leaving it a prose claim.
- Existing reaper tests carry forward unchanged; they already pin the exemption, the renewed-window
  decline, and the fail-loud unknown owner kind.
