# Upload-prefix orphan sweep — design (#1556)

- **Issue:** [#1556](https://github.com/randomparity/kdive/issues/1556)
- **ADR:** [ADR-0455](../adr/0455-upload-prefix-orphan-sweep.md)
- **Depends on:** [ADR-0453](../adr/0453-row-first-upload-reap.md) (the disclosure this closes),
  [ADR-0048](../adr/0048-external-build-artifact-ingestion.md) §6 (the upload window and its
  reaper), [ADR-0092](../adr/0092-image-rootfs-lifecycle.md) (the `images/` orphan scan this is
  modelled on)
- **Migration:** none.

## Problem

ADR-0453 made `reap_one_owner` row-first: phase 1 commits the `upload_manifests` row delete, phase
2 deletes the window's objects holding no lock and no connection. If phase 2 fails partway — a
`CategorizedError` per key, or an abort between the commit and the last delete — the objects survive
with **no** manifest row and **no** `artifacts` row.

Nothing reclaims them. `gc_expired_build_artifacts` is row-driven over `artifacts` and the reaper by
construction only ever deletes keys with no `artifacts` row, so a row-first orphan is structurally
invisible to it. `repair_leaked_images` is the only prefix-driven orphan scan and
`ObjectStore.list_image_objects` hardcodes the `images/` prefix. There is no bucket lifecycle rule
in the tree. The leak is storage-cost only, and it accumulates monotonically.

## What makes it fixable

The prefix survives the row. Upload keys are `owner_prefix(UPLOAD_TENANT, owner_kind, owner_id)`
from a single mint site — `local/runs/<run_id>/` and `local/investigations/<investigation_id>/` — so a
leaked object's owner is recoverable **from the key itself**, not merely from the owner tables. That
is the difference between an enumerable backlog and log archaeology.

## Acceptance criteria

- **AC-1** An object under `local/<kind>/<id>/` with no `artifacts` row and no `upload_manifests`
  row for its owner is reclaimed without operator action.
- **AC-2** An object belonging to a live (or re-minted) upload window is never reclaimed.
- **AC-3** A freshly written object is protected by a grace window compared in Postgres, never in
  Python, and that window clears the upload-window TTL so a just-reaped window's bytes are not
  reclaimed in the pass that reaped them.
- **AC-4** An object with a committed `artifacts` row is never reclaimed, whatever its age.
- **AC-5** A key that cannot be attributed to an owner is never reclaimed.
- **AC-6** A store fault during the sweep loses nothing: the next pass re-derives the same
  candidates, and the fault is visible on the ADR-0190 group-E error counter.
- **AC-7** A sweep scoped out of its own bucket by key-layout drift is distinguishable from a clean
  one, rather than reporting the same zero.
- **AC-8** One pass's reclaim work is bounded, so draining a backlog does not stall the rest of the
  reconciler catalog, and one permanently undeletable object does not starve the keys behind it.

## Design

### The sweep

A new reconciler repair, `repair_leaked_upload_objects`, in
`src/kdive/reconciler/cleanup/upload_orphans.py`:

1. LIST each upload root — `local/runs/` and `local/investigations/`, both halves derived (kinds
   from the reaper's table, tenant from the mint's shared constant) so neither can drift — with
   store mtimes.
2. Attribute each listed key to an owner by parsing it: exactly four `/`-separated components,
   `local/<kind>/<uuid>/<name>`, `<kind>` a known upload owner kind and `<uuid>` a parseable UUID.
   A key of any other shape is dropped, never deleted (AC-5).
3. Classify the whole listing in **one** query (below) into the reclaimable set.
4. For each reclaimable key, re-read its store mtime (a LIST scoped to the exact key) and re-run
   the *same* query for that key alone, immediately before the delete; delete only if it still
   classifies. Log one INFO per delete. A failed delete is logged, counted, and skipped; the pass
   raises once at the end. Each root examines at most `MAX_RECLAIMS_PER_ROOT` candidates.

### The predicate

One SQL statement, used for both the bulk classify and the per-key re-check so the two cannot drift:

```sql
SELECT c.key
FROM unnest(%s::text[], %s::timestamptz[], %s::text[], %s::uuid[])
     AS c(key, last_modified, owner_kind, owner_id)
WHERE c.last_modified < now() - %s
  AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.object_key = c.key)
  AND NOT EXISTS (SELECT 1 FROM upload_manifests m
                  WHERE m.owner_kind = c.owner_kind AND m.owner_id = c.owner_id)
```

Three fences, all evaluated in Postgres `now()` — and the third is not independent of the second:

- **`artifacts`** — the object is registered and owned by the catalog (AC-4).
- **`upload_manifests`** — the owner has *any* upload window, live or lapsed. A lapsed one is the
  reaper's to collect; a live or re-minted one owns these key names, because upload keys are
  owner-addressed (AC-2). The mint writes the manifest row before the presigned URL is issued, so
  no legitimate PUT can exist for a window whose row is absent.
- **the mtime threshold** — a presigned PUT may begin before the deadline and complete after it,
  which is routine. An in-flight PUT is not listed at all; a just-completed one is protected past
  its completion (AC-3). For the per-key re-check this mtime is **re-read from the store**, because
  the non-upload writers under `local/runs/` are object-before-row (a vmcore's `put_stream`, a
  `capture_traffic` retry) and have no row to protect them while their PUT is in flight.

### The threshold is `orphan_grace + upload_ttl`

The manifest fence lapses exactly when the reaper deletes the row, one TTL after the mint. So an
mtime threshold that merely equals `KDIVE_UPLOAD_TTL_SECONDS` leaves an object PUT promptly after
its mint reclaimable within seconds of the reap, and a TTL raised above it leaves the object
reclaimable *in the same pass that reaped it* — destroying ADR-0448's re-mint recovery, which
depends on the bytes outliving the row. Both values are read from config per pass and summed, so the
margin is a full orphan grace past the earliest possible reap at any TTL **this process is
configured with**. Defaults: 24h each.

That qualifier is the boundary: the TTL that governs when a row is reaped is the one the *server*
minted with, and the reconciler reads its own environment, where the setting's default makes an
unset variable indistinguishable from a deliberate 86400. Declaring `reconciler` on
`KDIVE_UPLOAD_TTL_SECONDS` surfaces it in `config validate` and the generated reference; it cannot
detect a one-sided rollout. The cost if that happens is a forced re-upload, not corruption.

`KDIVE_UPLOAD_ORPHAN_GRACE_SECONDS` is a real setting, mirroring the sibling image sweep's
`KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`, rather than a bare `ReconcileConfig` default. This repair
deletes irreversibly from a prefix that also holds non-upload objects, so an operator who finds it
removing live bytes needs a brake that is not a redeploy.

Both terms therefore declare `server` **and** `reconciler`. The reconciler runs this repair on its
loop, but it is an unconditional catalog entry, and `ops.reconcile_now` runs a full `reconcile_once`
in the *server* process — so the server executes the same irreversible deletes. `processes` does not
gate resolution, but it gates `config validate` and the generated operator reference, which is what
each process's environment is provisioned from; a brake declared for only one of the two leaves the
other sweeping at the default at the moment the brake is reached for.

### The store method

`ObjectStore.list_prefix_with_mtime(prefix)` returns `list[ObjectListing]`.
`list_image_objects()` becomes a one-line delegate over `images/`, so the pagination loop exists
once. `ImageSweepStore` keeps its narrow, prefix-free method deliberately: an image sweep should not
gain the authority to list an arbitrary prefix.

### Scoped-out detection

Zero deleted is also the healthy steady state, so the one failure that scopes this sweep out without
raising — a key layout the parser no longer recognizes — logs a WARNING naming the root and the
count when a root listed objects and attributed none (AC-7). The likeliest cause is removed rather
than only reported: both halves of the prefix are derived, the kinds from the reaper's table and the
tenant from `upload_manifest.UPLOAD_TENANT`, which the mint sites now share.

### Failure handling

A failed delete is logged, counted, and skipped; the pass raises once after the last root, so
`_run_repair_plan` still records it on the ADR-0190 error counter (AC-6, AC-8). The raise forfeits
the pass's count (`_run_repair_plan` records one only for a repair that returns), so the count is
logged at ERROR immediately before it. Aborting at the first
failure would let one permanently undeletable object — an S3 Object Lock hold, a per-key deny —
starve every candidate behind it and the whole second root on every pass forever, which is the leak
this repair exists to drain. Nothing is lost either way: this sweep commits nothing, so the next
pass re-derives the identical candidates.

### Bounding one pass

`MAX_RECLAIMS_PER_ROOT` bounds how many candidates one pass examines per root (AC-8). The
reconciler runs its catalog sequentially on one connection with no per-pass deadline, and every
examined candidate costs a LIST and a query whatever its outcome — so an unbounded drain of the
backlog accumulated since ADR-0453 would hold every other repair behind it. This is the drain-side
counterpart of ADR-0453 §4's cap on the reap side.

Two details matter. The budget is **per root** so a persistent fault under `local/runs/` cannot
spend a whole per-pass allowance on failures and leave `local/investigations/` unlisted forever. And
it charges every candidate that reaches the re-read, not only the deletes, because a declined
re-check costs the same two round trips — which is what two overlapping passes produce, the daemon
loop and an on-demand `ops.reconcile_now` both walking the same backlog.

### Cost, and what is deferred

Steady state with no leak is one LIST per root plus one query per root per pass, and each query runs
in its own short transaction so no snapshot is pinned across the blocking store calls. That is
cheaper *per object* than `repair_leaked_images`, which issues a query per listed object every pass
— but the comparison does not carry, because `images/` is bounded by the image catalog while
`local/runs/` grows for the life of the deployment. Two costs are therefore accepted here and filed
rather than argued away: the sweep materializes each root's whole listing (**#1569** — paging it
needs a paginating store API), and the `artifacts.object_key` anti-join has no usable index
(**#1570**), because the only one is partial on `owner_kind = 'investigations'` (migration 0076), so
the `local/runs/` root forces a table scan per classify.

## Non-goals

- **#1557** — the race where the *reaper's* phase-2 sweep deletes a re-minted or concurrently
  re-written object. That is a guard inside `_sweep_uncommitted_objects`, a different function. This
  change publishes `reclaimable_upload_keys` as the reusable per-key predicate that guard can call.
- **#1574** — the residual of this sweep's own per-key re-check: a PUT landing between the store
  mtime re-read and the `delete_object` is still destroyed. Closing it needs a fence the writer and
  the sweeper share (a conditional delete, or the owner-scoped lock #1557 needs anyway), not another
  unsynchronised re-check. The re-read closes the wide window; this is the round-trip remainder.
- A bucket lifecycle rule (option 2 in the issue): outside the tree, untestable here, and it would
  expire live windows unless tuned above `UPLOAD_TTL_SECONDS`.
- Any change to the reaper, the finalizes, the schema, or the MCP surface.
