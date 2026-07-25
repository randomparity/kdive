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

### 2. The committed row delete is the barrier that keeps phase 1's verdict valid

Computing the exemption before the split is safe **for the reaped window** because no key can
acquire an `artifacts` row for it after phase 1 commits: the only writers that insert one against
these keys are the two finalizes, and both require the manifest row phase 1 just deleted —
`_require_unreaped_window` rejects with `no_upload_manifest`, `complete_rootfs_upload` rejects for a
missing manifest. This is the substance of the decision, not merely a different order: object-first
has no such barrier available, because the state that authorizes the deletes is only durable once
the row delete commits.

The barrier is not unconditional, and this ADR does not claim it is. A **re-mint** creates a new
manifest row, which lifts the barrier for the window it opens; because upload keys are
owner-addressed, that new window reuses these same key names and its finalize can commit `artifacts`
rows against them. That is residual 2 in §Consequences, filed as #1557.

### 3. A failed key does not strand the rest, and does not raise

Phase 2 catches `CategorizedError` per key, logs it at WARNING with the key, and continues; a
non-zero failure count is then logged once at ERROR naming the owner and the counts. Raising would
be worse on every axis: the row is already durably gone, so the owner *is* reaped and there is
nothing to retry, and `repair_abandoned_uploads` has no per-candidate `try`, so one bad key would
abandon every later owner in the pass. `CategorizedError` is caught specifically — the category the
store wraps `BotoCoreError`/`ClientError` in, matching `_cleanup_chunks_and_manifest`'s precedent —
so a programming error still crashes and `CancelledError` still propagates.

Those two logs only cover the failure modes phase 2 can *observe*. The abort modes this ADR's
Context names as motivating — cancellation at shutdown, a lost connection, a process kill — unwind
past them, and by then the row that held the prefix is gone, so the leaked bytes would have no
derivable handle at all. `reap_one_owner` therefore logs the prefix and the key count at INFO
**before** the sweep begins, which is the last instant the prefix exists anywhere. That is the
recoverable record; the WARNING/ERROR pair is the report on top of it. Neither is decoration: per
§Consequences nothing sweeps that prefix, so the log is the only trace these objects ever existed.

The asymmetry with phase 1 is deliberate. `store.list_prefix` raises `CategorizedError` on the same
faults `store.delete` does, and phase 1 does **not** catch it — a listing failure ends the whole
pass. That is benign where a sweep failure is not: phase 1 aborts before the row delete commits, so
the transaction rolls back with nothing deleted and the next 30-second pass retries the same
candidates unchanged. Tolerance is bought only where a retry is impossible.

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
are.

**Phase 2 no longer excludes a re-mint, on either lane.** Object deletion outside the owner lock
means a re-mint can now interleave between the row-delete commit and the object deletes; upload keys
are owner-addressed (`local/runs/<id>/<name>`), so a re-minted window reuses the same key names and
the sweep — which deletes the phase-1 key list unconditionally, re-reading nothing — would remove
the new window's bytes. The trigger is just that: **one re-minted PUT landing on a key before the
sweep reaches that key's `delete_object`.** The finalize may commit arbitrarily later and still be
corrupted, because `_require_unreaped_window` compares the *new* window's deadline against the one
that finalize itself validated, and they match. It is the same corruption class this ADR removes,
and it is narrower only in the sense that the old defect needed nothing more than a finalize
straddling the deadline. It is not exotic: a re-mint is the *documented recovery* from a reap
(ADR-0448), so it is on the recovery path, not off it. In practice the sweep is a handful of
`delete_object` calls and completes long before an agent can re-mint and re-upload, so reaching it
needs a stalled or degraded store — but that is exactly the condition this change exists for.

This also **puts the investigations lane in scope**, which the Context excused for the *original*
defect and cannot excuse here: `complete_rootfs_upload`'s single-transaction `INVESTIGATION`-locked
finalize serialised against a reap only while the reap held that lock, and phase 2 does not.

Filed as **#1557**, with the options costed there rather than guessed at here. None is free:
re-reading `upload_manifests` once before the sweep is one indexed query but sits *before* the
delete loop, which is the exposed interval; a per-key re-check (`repair_leaked_images`' precedent)
shrinks the window to one check→delete gap but returns a query per key and a database connection to
the phase #1554 wants free of both; a store-mtime grace (also `repair_leaked_images`' precedent)
closes it outright but needs a prefix-parameterised sibling of `list_image_objects` and permanently
leaks any object whose presigned PUT began before the deadline and completed after it — a routine
path, not a rare one, traded against a rare corruption. An identity-conditioned delete does not
close it at all: `Last-Modified` has one-second granularity and a byte-identical re-upload carries
the same etag.

`repair_abandoned_uploads` still has no per-candidate `try`, so **any** fault during one owner's
phase 1 ends the pass — `CategorizedError` included, and a failing `store.list_prefix` is the most
likely one in the store outage this change is built for. That is unchanged behaviour, it is bounded
(`_run_repair_plan` isolates each repair, so only this repair's pass is lost), and it costs nothing:
phase 1 aborts before the row delete commits, so the next 30-second pass re-reads the same
candidates with no objects deleted.

The two-phase split is also the shape #1554's concurrent sweep needs: a short locked transaction
that decides, and a phase touching only the object store, fannable out without holding a lock or a
pooled connection.

No schema, no migration, no config setting, no MCP or RBAC surface, no metric, and no change to
either finalize path. Not an AI surface.
