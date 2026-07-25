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

Computing the exemption before the split is safe because no key can acquire an `artifacts` row
after phase 1 commits: the only writers that insert one against these keys are the two finalizes,
and both require the manifest row phase 1 just deleted — `_require_unreaped_window` rejects with
`no_upload_manifest`, `complete_rootfs_upload` rejects for a missing manifest. This is the substance
of the decision, not merely a different order: object-first has no such barrier available, because
the state that authorizes the deletes is only durable once the row delete commits.

### 3. A failed key does not strand the rest, and does not raise

Phase 2 catches `CategorizedError` per key, logs it at WARNING with the key, and continues; a
non-zero failure count is then logged once at ERROR naming the owner and the counts. Raising would
be worse on every axis: the row is already durably gone, so the owner *is* reaped and there is
nothing to retry, and `repair_abandoned_uploads` has no per-candidate `try`, so one bad key would
abandon every later owner in the pass. `CategorizedError` is caught specifically — the category the
store wraps `BotoCoreError`/`ClientError` in, matching `_cleanup_chunks_and_manifest`'s precedent —
so a programming error still crashes and `CancelledError` still propagates. The ERROR log is not
decoration: per §Consequences those bytes are unreferenced and nothing sweeps them, so it is the
only signal that exists.

## Consequences

Two residuals are disclosed and neither is fixed here.

**The leak is real and unswept.** A phase-2 failure — or a crash between the commit and the last
delete — leaves objects under `local/<kind>/<id>/` with no manifest row and no `artifacts` row.
Nothing in this tree will ever reclaim them: `gc_expired_build_artifacts` enumerates `artifacts`
rows, the `images/` orphan scan is scoped to a different prefix, and there is no lifecycle rule.
The claim that #768's reaper covers this is false and is not relied on. Filed as a follow-up for an
upload-prefix orphan sweep (or a bucket lifecycle rule). Row-first converts a correctness bug into
a storage-cost bug that is invisible until someone looks at the bucket — accepted, because a
dangling `kernel_ref` on a `succeeded` Run is not recoverable by any later sweep and leaked bytes
are.

**Phase 2 no longer excludes a re-mint.** Object deletion outside the owner lock means a re-mint can
now interleave between the row-delete commit and the object deletes; upload keys are owner-addressed
(`local/runs/<id>/<name>`), so a re-minted window reuses the same key names and the deferred delete
would remove the new window's bytes. It is strictly narrower than what it replaces — it needs a full
create-upload → PUT → finalize cycle to complete inside the time it takes to issue N `delete_object`
calls, against the old defect's requirement that a finalize merely straddle the deadline — but it is
the same corruption class and is filed rather than only noted. Making the deletes conditional on an
identity captured in phase 1 would narrow it further at the cost of a HEAD per key and a widened
`UploadStore` port, and would still not close it: `Last-Modified` has one-second granularity and a
byte-identical re-upload carries the same etag.

`repair_abandoned_uploads` still has no per-candidate `try`, so a non-`CategorizedError` fault
during one owner's phase 1 still ends the pass; that is unchanged behaviour and the next 30-second
sweep re-reads the same candidates.

The two-phase split is also the shape #1554's concurrent sweep needs: a short locked transaction
that decides, and a phase touching only the object store, fannable out without holding a lock or a
pooled connection.

No schema, no migration, no config setting, no MCP or RBAC surface, no metric, and no change to
either finalize path. Not an AI surface.
