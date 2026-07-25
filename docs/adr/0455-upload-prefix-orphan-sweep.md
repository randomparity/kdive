# ADR 0455 — Sweep the upload prefix for objects no row can reach

- **Status:** Accepted
- **Date:** 2026-07-25
- **Closes:** [ADR-0453](0453-row-first-upload-reap.md) §Consequences' **first** disclosed residual
  — the unswept leak row-first reaping trades a correctness bug for. ADR-0453's decisions are
  retained unchanged; its **second** residual (#1557) is explicitly not addressed here.
- **Depends on:** [ADR-0048](0048-external-build-artifact-ingestion.md) §6 (the `upload_manifests`
  window, its prefix, and its reaper), [ADR-0104](0104-chunked-external-upload-reassembly.md) §7
  (the leftover-chunk backstop), [ADR-0092](0092-image-rootfs-lifecycle.md) (the `images/` orphan
  scan whose shape this reuses), [ADR-0190](0190-expanded-operational-metrics.md) (the group-E
  error counter this repair's failures land on).
- **Spec:** [`../specs/2026-07-25-upload-prefix-orphan-sweep-1556-design.md`](../specs/2026-07-25-upload-prefix-orphan-sweep-1556-design.md)

## Context

ADR-0453 §Consequences discloses, and declines to fix, the cost of row-first reaping: when phase 2
fails partway — a `CategorizedError` per key, a crash or cancellation between the row-delete commit
and the last `delete_object` — the window's objects survive with no `upload_manifests` row and no
`artifacts` row, and nothing in this tree reclaims them.

That was checked rather than assumed, and it holds. `gc_expired_build_artifacts` enumerates
`artifacts` rows; the reaper by construction only ever deletes keys that have **no** `artifacts`
row, so a row-first orphan is not merely missed by #768's reaper, it is structurally invisible to
it. `repair_leaked_images` is the only prefix-driven orphan scan in the tree and
`ObjectStore.list_image_objects` hardcodes `images/`. There is no bucket lifecycle rule in `src/`
or in the deployment manifests. The consequence is storage cost with no correctness impact —
invisible until someone looks at the bucket, and monotonically accumulating.

What makes it cheap to unwind is that the prefix outlives the row. Upload keys are
`owner_prefix(_TENANT, owner_kind, owner_id)` from a single mint site, so a leaked object's owner is
recoverable from the **key**, not just from the owner tables — which is why this is an enumeration
and not log archaeology. ADR-0453 §Decision 4 already caps how much backlog one degraded pass can
add; this ADR drains it.

## Decision

### 1. A prefix sweep over the upload roots, attributing each key to its owner

`repair_leaked_upload_objects` lists `local/runs/` and `local/investigations/` — the two roots
derived from the reaper's own owner-kind table, so the sweep's scope cannot drift from what the
reaper reaps — with store mtimes, and attributes each listed key by parsing it. A key qualifies only
if it splits into exactly four components, `local/<kind>/<uuid>/<name>`, with a known upload owner
kind and a parseable UUID. Every other shape is dropped without a delete.

Parsing the key rather than walking `runs`/`investigations` is deliberate and is not the shape
ADR-0453 §Consequences sketched. It is one LIST per root instead of one LIST per owner row, and it
also reaches an orphan whose owner row has since been deleted — which a walk of the owner tables
would leave behind permanently. Nothing is lost by it, because the key carries the same
`(kind, id)` the owner row would have supplied.

### 2. Three fences, all evaluated in Postgres, and one statement so they cannot drift

An object is reclaimable only if all three hold:

- **No `artifacts` row references the key.** The object is unregistered; nothing in the catalog can
  reach it.
- **The owner holds no `upload_manifests` row at all** — not "no live one". A lapsed window is the
  reaper's to collect and this sweep must not race it; a live or **re-minted** window owns these
  key names, because upload keys are owner-addressed. This is the fence that keeps a re-mint safe,
  and it is load-bearing: a re-mint is the *documented* recovery from a reap (ADR-0448), so it sits
  on the recovery path rather than off it. It is a sound fence because the mint writes the manifest
  row before any presigned URL exists, so no legitimate PUT can be in flight for a window whose row
  is absent.
- **The object's store mtime is older than a grace**, compared against `now() - grace` in Postgres.

The grace is required for correctness, not polish. A presigned PUT may **begin** before the window's
deadline and **complete** after it; the reaper can therefore reap the row while bytes are still
landing, and prefix membership alone would destroy a live upload. An in-flight PUT is not listed at
all (S3 publishes the object on completion), and a just-completed one is protected for `grace` past
its completion — during which the finalize registers its `artifacts` row, or the re-mint that
authorized it re-arms the manifest fence.

All three are one SQL statement over `unnest(...)` of the candidate arrays, and that same statement
serves both the bulk classify and the per-key re-check. Two hand-kept copies of a
"safe to delete" predicate is exactly the drift that makes a reclaim sweep dangerous, so there is
one.

### 3. Classify in bulk, re-check per key immediately before the delete

One query classifies the whole listing; each reclaimable key is then re-run through the identical
predicate alone, immediately before its `delete_object`. The re-check is `repair_leaked_images`'
precedent and it shrinks — it does not close — the window between deciding and deleting: a finalize
or a re-mint that commits in that gap protects its object.

The residual it leaves is narrow and stated rather than implied: a mint that commits between the
re-check and the `delete_object`, followed by a PUT landing on that key inside the same gap, is
deleted. That needs a re-mint to interleave inside a single check→delete gap for a key that has
already been rowless and manifest-less for a full grace period.

Bulk-then-recheck is also the cost decision. Steady state with no leak is one LIST and one query per
root per pass; the per-key round trips are paid only for keys actually being deleted, which is
normally none. `repair_leaked_images` issues a query per listed object on every pass, so this is
strictly cheaper than the precedent it copies, and it stays inside the single `conn` the repair seam
hands it — it opens no second connection and cannot press on the `max_size=10` pool the fleet
snapshot shares.

### 4. Nothing is caught

A store fault propagates out of this repair. `_run_repair_plan` isolates it, logs it, and records it
in `failures`, which is the sole input to the ADR-0190 group-E error counter — so a bucket policy
without `s3:DeleteObject` shows up as an error rather than as a quiet zero.

This is the opposite of the reaper's per-key tolerance, and the asymmetry has a reason. The reaper
tolerates because its row delete has **already committed**: the owner is reaped, there is nothing to
retry, and raising would abandon later owners over one bad key. Here nothing is committed at all —
the candidates are re-derived from the store and the database on the next pass, with the same
verdict — so aborting costs one pass and buys the alert. It is also `repair_leaked_images`'
treatment, which this sweep is otherwise modelled on.

### 5. One prefix-parameterised listing primitive; `ImageSweepStore` stays narrow

`ObjectStore.list_prefix_with_mtime(prefix)` is the paginated key+mtime listing, and
`list_image_objects()` becomes a one-line delegate over `images/` so the pagination loop exists
once. The `ImageSweepStore` **port** deliberately keeps its prefix-free method: an image sweep has
no business being able to list an arbitrary prefix, and widening the port to avoid one delegating
line would trade a real authority bound for a cosmetic one.

The grace is a `ReconcileConfig` field defaulting to 24h, not an environment setting — the shape
`build_artifact_retention` and `dump_volume_grace` already use. 24h sits far above every legitimate
rowless interval under these roots (a `capture_traffic` pcap's PUT and its row share one
transaction; a vmcore object is PUT minutes before `finalize_capture` inserts its rows), and the
asymmetry is chosen: a day of extra leak is a cost bug, a deleted live object is a correctness bug.

## Consequences

The leak ADR-0453 accepted is now drained without operator action, and its acceptance argument —
"a dangling `kernel_ref` on a `succeeded` Run is not recoverable by any later sweep and leaked bytes
are" — is discharged rather than left as an assertion. ADR-0453 §Consequences' first residual is
closed; **its second is not**, and this ADR does not narrow it.

**#1557 is untouched and is not made easier to mistake for closed.** The race it names lives in
`_sweep_uncommitted_objects`, where the *reaper* deletes a phase-1 key list without re-reading
anything. This sweep is a different function with a different candidate set, and it cannot protect
a key the reaper has already doomed. What this change hands #1557 is the predicate:
`reclaimable_upload_keys` is public, takes a connection and a candidate list, and is the per-key
re-check ADR-0453 §Consequences costed as "a per-key re-check (`repair_leaked_images`' precedent)"
— wiring it into `_sweep_uncommitted_objects` is a call, not a rewrite. It also supplies the
mtime-bearing listing that ADR-0453 said a store-mtime grace "needs a prefix-parameterised sibling
of `list_image_objects`" for. Both options #1557 costed are now build-ready; choosing between them,
and paying the connection they cost the phase #1554 wants connection-free, remains #1557's call.

**The sweep is scoped to the owner prefix, not to upload keys.** `local/runs/<id>/` holds every
run-scoped object, not only uploads — `control.capture_traffic`'s pcap and the vmcore objects live
there too. They are protected by their `artifacts` rows once registered and by the grace before
that, which is sound because both register within seconds of their PUT. It does mean a *future*
writer that puts a run-owned object and leaves it rowless for more than 24h by design would be
swept; that constraint is the same one `repair_leaked_images` places on the image prefix, and it is
recorded here so it is a known rule rather than a surprise.

**A grace this long is a deliberate detection delay.** An orphan is not reclaimed for 24h after its
last write. That is the correct direction for an irreversible delete, but it means the bucket-size
signal an operator watches lags the leak by a day.

**Only these two roots are swept.** `local/systems/`, `local/reports/`, the `remote-libvirt/` and
`fault-inject/` tenants, and `images/` are out of scope — the first two because no upload window
ever mints into them, the rest because they are other tenants' or other sweeps' namespaces.

No schema, no migration, no config setting, no MCP or RBAC surface, no change to either finalize
path, and no change to the reaper. One new `repair_kind` (`leaked_upload_objects`) joins
`ALL_REPAIR_KINDS` and so the ADR-0190 repairs counter; no new metric and no new report field. Not
an AI surface.
