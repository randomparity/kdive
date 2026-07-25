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

The prefix survives the row. Upload keys are `owner_prefix(_TENANT, owner_kind, owner_id)` from a
single mint site — `local/runs/<run_id>/` and `local/investigations/<investigation_id>/` — so a
leaked object's owner is recoverable **from the key itself**, not merely from the owner tables. That
is the difference between an enumerable backlog and log archaeology.

## Acceptance criteria

- **AC-1** An object under `local/<kind>/<id>/` with no `artifacts` row and no `upload_manifests`
  row for its owner is reclaimed without operator action.
- **AC-2** An object belonging to a live (or re-minted) upload window is never reclaimed.
- **AC-3** A freshly written object is protected by a grace window compared in Postgres, never in
  Python.
- **AC-4** An object with a committed `artifacts` row is never reclaimed, whatever its age.
- **AC-5** A key that cannot be attributed to an owner is never reclaimed.
- **AC-6** A store fault during the sweep loses nothing: the next pass re-derives the same
  candidates, and the fault is visible on the ADR-0190 group-E error counter.

## Design

### The sweep

A new reconciler repair, `repair_leaked_upload_objects`, in
`src/kdive/reconciler/cleanup/upload_orphans.py`:

1. LIST each upload root — `local/runs/` and `local/investigations/`, derived from the reaper's own
   owner-kind table so the two cannot drift — with store mtimes.
2. Attribute each listed key to an owner by parsing it: exactly four `/`-separated components,
   `local/<kind>/<uuid>/<name>`, `<kind>` a known upload owner kind and `<uuid>` a parseable UUID.
   A key of any other shape is dropped, never deleted (AC-5).
3. Classify the whole listing in **one** query (below) into the reclaimable set.
4. For each reclaimable key, re-run the *same* query for that key alone, immediately before the
   delete, and delete only if it still classifies. Log one INFO per delete.

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

Three independent fences, all evaluated in Postgres `now()`:

- **`artifacts`** — the object is registered and owned by the catalog (AC-4).
- **`upload_manifests`** — the owner has *any* upload window, live or lapsed. A lapsed one is the
  reaper's to collect; a live or re-minted one owns these key names, because upload keys are
  owner-addressed (AC-2). The mint writes the manifest row before the presigned URL is issued, so
  no legitimate PUT can exist for a window whose row is absent.
- **the mtime grace** — a presigned PUT may begin before the deadline and complete after it, which
  is routine. An in-flight PUT is not listed at all; a just-completed one is protected for
  `grace` past its completion (AC-3).

### The grace

`DEFAULT_UPLOAD_ORPHAN_GRACE = 24h`, a `ReconcileConfig` field, not an env setting — the same shape
as `build_artifact_retention` and `dump_volume_grace`. 24h is far above every legitimate rowless
interval this prefix has (a `capture_traffic` pcap PUT and its row share one transaction; a vmcore
object is PUT minutes before `finalize_capture` inserts its rows), and the asymmetry is deliberate:
leaking bytes for an extra day is a cost bug, deleting live bytes is a correctness bug.

### The store method

`ObjectStore.list_prefix_with_mtime(prefix)` returns `list[ObjectListing]`.
`list_image_objects()` becomes a one-line delegate over `images/`, so the pagination loop exists
once. `ImageSweepStore` keeps its narrow, prefix-free method deliberately: an image sweep should not
gain the authority to list an arbitrary prefix.

### Failure handling

Unlike the reaper's phase 2, this sweep catches nothing. Nothing here is irreversible-once-committed
— a store fault aborts the pass with the same candidates derivable next pass — so letting it
propagate gives `_run_repair_plan` the error counter and costs nothing (AC-6). This is
`repair_leaked_images`' treatment, and the reason it differs from the reaper is that the reaper's
row delete has already committed by the time its sweep runs.

### Cost

Steady state with no leak is one LIST per root plus one query per root per pass. That is strictly
cheaper than `repair_leaked_images`, which issues a query **per listed object** every pass.

## Non-goals

- **#1557** — the race where the *reaper's* phase-2 sweep deletes a re-minted or concurrently
  re-written object. That is a guard inside `_sweep_uncommitted_objects`, a different function. This
  change publishes `reclaimable_upload_keys` as the reusable per-key predicate that guard can call.
- A bucket lifecycle rule (option 2 in the issue): outside the tree, untestable here, and it would
  expire live windows unless tuned above `UPLOAD_TTL_SECONDS`.
- Any change to the reaper, the finalizes, the schema, or the MCP surface.
