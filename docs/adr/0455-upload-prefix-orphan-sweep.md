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

`repair_leaked_upload_objects` lists `local/runs/` and `local/investigations/` — both halves of
each root derived rather than written out, the kinds from the reaper's own owner-kind table and the
tenant from the constant the mint sites share, so the sweep's scope cannot drift from what the
reaper reaps or from where the mint writes (§4) — with store mtimes, and attributes each listed key
by parsing it. A key qualifies only
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
- **The object's store mtime is older than `orphan_grace + upload_ttl`**, compared against
  `now() - threshold` in Postgres.

The mtime fence is required for correctness, not polish. A presigned PUT may **begin** before the
window's deadline and **complete** after it; the reaper can therefore reap the row while bytes are
still landing, and prefix membership alone would destroy a live upload. An in-flight PUT is not
listed at all (S3 publishes the object on completion), and a just-completed one is protected past
its completion — during which the finalize registers its `artifacts` row, or the re-mint that
authorized it re-arms the manifest fence.

**The upload TTL is part of the threshold, not padding.** The manifest fence lapses exactly when
the reaper deletes the row, a TTL after the mint — so the two fences are not independent, and a
threshold that only *equals* `KDIVE_UPLOAD_TTL_SECONDS` gives an object PUT promptly after its mint
a post-reap margin of seconds, while a TTL raised above it gives a *negative* margin: at the moment
the reaper commits, `mtime < now() - grace` already holds, and this repair runs directly after the
reaper in the same pass. That would reclaim a window's bytes in the pass that reaped them and
destroy ADR-0448's documented re-mint recovery, which depends on the bytes outliving the row. Both
values are read from config per pass and summed, so the margin is a full `orphan_grace` past the
earliest reap of any window an object could have belonged to, at any TTL **this process is
configured with**. The defaults are 24h each.

That last qualifier is the guarantee's real boundary and is not a code property. The TTL that
governs when a row is actually reaped is the one the *server* minted with; the reconciler reads its
own environment, and the setting's default means an unset variable is indistinguishable from a
deliberate 86400. So a partial rollout that raises the server's TTL to 7 days and leaves the
reconciler's environment alone puts the margin back underwater. Declaring `reconciler` on
`KDIVE_UPLOAD_TTL_SECONDS` is what surfaces that in `config validate` and in the generated operator
reference an environment is provisioned from; it cannot detect the skew. The cost when it happens is
a forced re-upload, not corruption — a finalize against a reaped window is already rejected.

All three are one SQL statement over `unnest(...)` of the candidate arrays, and that same statement
serves both the bulk classify and the per-key re-check. Two hand-kept copies of a
"safe to delete" predicate is exactly the drift that makes a reclaim sweep dangerous, so there is
one.

### 3. Classify in bulk, re-check per key immediately before the delete

One query classifies the whole listing; each reclaimable key is then re-run through the identical
predicate alone, immediately before its `delete_object`. The re-check is `repair_leaked_images`'
precedent and it shrinks — it does not close — the window between deciding and deleting: a finalize
or a re-mint that commits in that gap protects its object.

**The object's mtime is re-read from the store for that re-check, not carried from the listing**,
and this is where the precedent stops applying. `repair_leaked_images` can re-check the row alone
because ADR-0092 makes an image publish **row-before-object**, so a row is always in place before
any bytes exist. Under `local/runs/` the ordering is reversed for the non-upload writers
§Consequences puts in scope: a vmcore's multi-GiB `put_stream` and a `capture_traffic` retry's
re-PUT both land at a deterministic, reusable key name minutes before `finalize_capture`'s row
commits. For the length of that PUT the object has **no row to protect it** and its mtime is the
only fence — so re-checking the *listed* mtime would find it stale, and the sweep would delete
bytes that had just been written and leave `finalize_capture` committing rows against an object
that no longer exists. The re-read is a LIST scoped to the exact key, paid only for keys actually
being deleted, and an exact-match filter over the result — which is what makes it exact, because a
LIST on a key also returns every key that key prefixes. For a chunked window's **base** key those
siblings are its `<base>.partNNNN` parts (ADR-0104 §1), and a row-first reap that failed partway
leaves base and parts rowless together, so that re-read costs a round trip per 1000 parts rather
than the one this section's cost model assumes. Correct, not O(1); #1575 tracks making it both.

> **Amended by [ADR-0496](0496-orphan-sweep-re-read-is-a-head.md) (#1575).** The re-read is a
> single `store.head`, not a LIST and a filter, so it is one round trip for every key shape and
> the exactness is structural rather than filtered. It is also the sweep's only `head_object`,
> which makes it a per-key failure site in its own right — §5's fourth. The
> `re-read → re-check → delete` ordering and the residual the next paragraph states are
> unchanged.

The residual it leaves is narrow and stated rather than implied: a PUT that lands between that
re-read and the `delete_object` is deleted. That is a single re-read→delete gap, for a key that has
been rowless, manifest-less, and unwritten for the whole threshold. It does **not** require a
re-mint to reach — the vmcore keys are deterministic per `(run, method)` and involve no upload
window at all, so a capture retried more than a threshold after an attempt that PUT the core and
died before `finalize_capture` is the cheapest way in. Saying "a re-mint has to interleave" would
make the residual sound rarer than it is. It is filed as **#1574** rather than left unowned — a
disclosed correctness residual with no tracking issue is indistinguishable from an unnoticed one,
and #1574 is the sibling of #1557, which is this same race on the reaper's side.

Bulk-then-recheck is also the cost decision, and the honest version of it is not "cheaper than the
precedent". Steady state with no leak is one LIST and one query per root per pass; the per-key round
trips are paid only for keys actually being deleted, which is normally none. `repair_leaked_images`
issues a query per listed object every pass, so this is cheaper *per object* — but that comparison
does not carry, because `images/` is bounded by the image catalog while `local/runs/` grows for the
life of the deployment (a vmcore per crashing run, pcaps, chunk parts). This sweep materializes each
root's whole listing and passes it as one array-valued parameter, so both scale linearly with a
bucket that never shrinks, every 30 seconds. It is adequate at the scale this repair is being
shipped into and it is not adequate forever; paging the sweep is filed as **#1569** rather than
asserted away here. Each query does run in its own short transaction, so no snapshot is pinned
across the blocking LISTs and deletes — but the repair seam still holds one of the `max_size=10`
pool's slots checked out for the whole sweep, which is #1554's to restructure, not a property this
change can claim.

### 4. A silently scoped-out sweep is distinguishable from a healthy one

Zero deleted is this repair's healthy steady state, so a sweep that has been scoped out of the
bucket it exists to drain reports exactly what a clean one reports. The condition that can cause
that without raising is a key layout `_attribute` no longer recognizes, and it is distinguishable —
objects listed, none attributed — so that case logs a WARNING naming the root and the count. It is
not a general per-fence counter: the other fences declining a key is the sweep working.

The same reasoning removes the more likely cause rather than only reporting it. Both halves of the
swept prefix are derived: the owner kinds from the reaper's table, and the tenant from
`upload_manifest.UPLOAD_TENANT`, which the two mint sites now share instead of each holding their
own `_TENANT = "local"` literal. A fourth copy in this module would have been the drift this
warning exists to catch.

### 5. A failed key is skipped and counted; the pass raises once at the end

A store fault must be visible: `_run_repair_plan` records a raising repair in `failures`, the sole
input to the ADR-0190 group-E error counter, so a bucket policy without `s3:DeleteObject` shows up
as an error rather than as a quiet zero. But raising *at the first failed key* would be wrong in the
one direction that matters here. A **persistent** per-object fault — an S3 Object Lock retention or
legal hold on a single orphan, a deny scoped to one key — would abort at that same key on every
30-second pass forever, so every candidate behind it in listing order and the whole second root
would never be reclaimed. That is the leak this repair exists to drain, resuming unbounded behind
one stuck object, with a repeating error indistinguishable from a transient blip.

So the failed key is logged and skipped, the count travels to the end of the pass, and the repair
raises once — `repair_abandoned_uploads`' shape, adopted for a different reason than the reaper has
for it. The reaper *must* tolerate because its row delete has already committed and there is nothing
to retry; this sweep commits nothing and could safely abort, but abort is what turns one stuck
object into a permanent leak.

A root has three failure sites, and all three are skipped and counted rather than allowed to end the
pass. Besides the per-key delete, a failing `list_prefix_with_mtime` and a failing bulk classify each
end **that root** immediately — with no listing there is no candidate set to be partial about, with
no classify nothing is known to be safe to delete — but the rationale for abandoning the root does
not reach the sibling, whose candidate set is wholly independent.

Both are root-correlated in practice, which is what makes aborting on them a permanent starvation
rather than a transient one. Root order is fixed at import with `local/runs/` first. A scoped
`s3:ListBucket` deny is the list-side twin of the per-prefix `s3:DeleteObject` deny §6 makes the
budget per root to survive. The classify is the sharper case: `local/runs/` is the larger,
faster-growing root, its `artifacts` anti-join has no usable index (#1570) and its whole listing goes
in as one array parameter (#1569), so a role-level `statement_timeout` fires on that root's scan and
not on the smaller root's — the root most likely to fail is structurally the one gating the other,
and the two deferred cost items make that more likely over time. Aborting would leave
`local/investigations/` — the rootfs upload lane's root — unswept on every pass for as long as the
fault persisted, which is the starvation this whole section exists to prevent, arriving by the two
paths the budget does not cover.

A fault that is *not* root-scoped still ends the pass, and ends it *through the same count-logging
path*, because by then a root may already have deleted irreversibly: that path catches every abort,
not only a store one, so a dropped pool connection out of the classify and cancellation at shutdown
arrive the same way and cost the same record.

Raising forfeits the pass's reclaimed count: `_run_repair_plan` records a count only for a repair
that *returns*, so a pass that deleted 500 objects and then hit one object-lock hold reports zero on
the ADR-0190 repairs counter. That is the trade ADR-0453 §3 already took for the reaper — the count
is a gauge, the raise is the alert — but it lands harder here, because a persistently undeletable
key would pin a working drain's gauge at zero indefinitely, and it is the opposite of the treatment
`reconcile_once`'s docstring records for the catalog's other irreversible repair
(`_repair_leaked_domains` catches per domain and keeps its count). The seam offers one or the other
and the alert is the one a permanent leak needs, so the count is written to the log as an ERROR
immediately before the raise instead of being lost with it.

### 6. Each root examines at most `MAX_RECLAIMS_PER_ROOT` candidates

> **Amended by [ADR-0496](0496-orphan-sweep-re-read-is-a-head.md) (#1575) and #1570.** The
> per-candidate cost is now a HEAD and a query, and that query is an index scan since migration
> `0081` added the `artifacts (object_key)` btree. The budget itself is unchanged and was not
> re-tuned.

Each examined candidate costs a LIST and a query whatever its outcome, and that query is the
unindexed `artifacts` anti-join filed as #1570 — so the cost is per key, not per pass, and the
disclosure in §Consequences that prices the steady state does not price a drain. The reconciler runs
its catalog strictly sequentially on one connection with no per-pass deadline, so an unbounded drain
would hold allocation expiry, orphaned-System repair, dead-session reaping, and domain reaping
behind it for however long it took. The first pass after this ships is the largest this code will
ever run, against a backlog accumulating since ADR-0453. The threshold is no substitute: it decides
*which* keys become candidates, not how many are processed once they do, and raising it does not
shorten a pass already in flight. ADR-0453 §4 put this brake on the reap side, capping a degraded
pass at one owner's leak; this is the same brake on the drain side.

Two details of the budget are load-bearing rather than incidental.

**It is per root, not per pass**, because a budget spent on *failures* would otherwise re-open the
starvation §5 exists to prevent. A scoped persistent fault — an object lock over a prefix, a
per-prefix `s3:DeleteObject` deny — covering a budget's worth of keys under `local/runs/` would
consume a per-pass budget entirely, on every pass forever, and `local/investigations/` would never
be listed at all. Per root, the stuck root spends its own allowance and the other still drains.

**It charges every candidate that reaches the re-read**, not only the ones deleted. A declined
re-check costs the same LIST and query as a delete, and two overlapping passes produce exactly that:
`ops.reconcile_now` builds its own config and runs a full `reconcile_once` while the daemon loop is
running, so whichever finishes second finds every object already gone and would re-read the entire
backlog for nothing.

What the budget still cannot fix is ordering *within* a root: a persistent fault on keys that sort
early defers that root's tail on every pass until the fault is cleared. The stuck keys' own bytes
are already leaked by the fault itself, so what this costs is the deferral of the keys behind them,
and §5's raise fires every pass while it lasts. The budget is a module constant rather than a
setting because it bounds a loop rather than expressing a policy.

### 7. One prefix-parameterised listing primitive; `ImageSweepStore` stays narrow

`ObjectStore.list_prefix_with_mtime(prefix)` is the paginated key+mtime listing, and
`list_image_objects()` becomes a one-line delegate over `images/` so the pagination loop exists
once. The `ImageSweepStore` **port** deliberately keeps its prefix-free method: an image sweep has
no business being able to list an arbitrary prefix, and widening the port to avoid one delegating
line would trade a real authority bound for a cosmetic one.

### 8. Both threshold terms are settings, resolved per pass

The threshold's two terms are both real settings resolved per pass —
`KDIVE_UPLOAD_ORPHAN_GRACE_SECONDS` and `KDIVE_UPLOAD_TTL_SECONDS`, 86400s each — rather than
`ReconcileConfig` defaults in the shape `build_artifact_retention` and `dump_volume_grace` use.
§Consequences records why that exception is taken. Both declare **`server` and `reconciler`**: the
reconciler runs the sweep on its loop and the server runs a full `reconcile_once` on demand via
`ops.reconcile_now`, so a brake an operator raises on only one of them leaves the other deleting at
the default — which for an irreversible delete is the brake failing exactly when it is reached for.

The brake engages on a **restart**, and the record says so rather than implying otherwise.
`Registry.load` snapshots `KDIVE_*` once at the process bootstrap, so resolving the two terms per
pass reads the same frozen values forever; and no operator can mutate a running process's
environment from outside it, so no resolution strategy inside this repair could have bought a live
brake. Resolving them here rather than at `ReconcileConfig` construction is still worth it — both
terms live in one place, declared, validated and documented, instead of as provider-shaped defaults
— but a restart is the price, and it is a far cheaper one than the image rebuild and redeploy a
hard-coded constant would demand.
48h at the defaults sits far above every legitimate rowless interval under these roots, and the
asymmetry is chosen: an extra day of leak is a cost bug, a deleted live object is a correctness bug.

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
there too. They are protected by their `artifacts` rows once registered, by the threshold
before that, and — for the interval between their PUT completing and their row committing, which
for a vmcore is minutes rather than seconds — by §3's store-side mtime re-read, which exists for
exactly these object-before-row writers. It does mean a *future* writer that puts a run-owned object
and leaves it rowless past the threshold **by design** would be swept; that constraint is the same
one `repair_leaked_images` places on the image prefix, and it is recorded here so it is a known rule
rather than a surprise.

**A threshold this long is a deliberate detection delay.** An orphan is not reclaimed until an
orphan grace plus an upload TTL — 48h at the defaults — past its last write. That is the correct direction for an irreversible delete, but it means the bucket-size
signal an operator watches lags the leak by a day.

**Only these two roots are swept.** `local/systems/`, `local/reports/`, the `remote-libvirt/` and
`fault-inject/` tenants, and `images/` are out of scope — the first two because no upload window
ever mints into them, the rest because they are other tenants' or other sweeps' namespaces.

**The `artifacts.object_key` anti-join has no index to use.** The only index on that column is
`artifacts_investigations_object_key_uniq`, which is *partial* — `WHERE owner_kind =
'investigations'` (migration 0076) — so the `local/runs/` root, the larger and faster-growing of the
two, forces Postgres to scan `artifacts` once per classify. That is a repeating scan of a table that
grows with every run, on every pass, in steady state with zero leak. "No migration" is therefore a
true statement about this diff and a misleading one about its cost; the index is filed as **#1570**.
It is deliberately not taken here: the sweep is correct without it, and a schema change belongs in a
change whose subject is the schema. The listing side of the same cost is **#1569**.

No schema, no migration, no MCP or RBAC surface, no change to either finalize path, and no change to
the reaper. One new setting, `KDIVE_UPLOAD_ORPHAN_GRACE_SECONDS` — an exception to the
`build_artifact_retention` precedent taken on purpose, because this repair deletes irreversibly from
a prefix that holds non-upload objects and an operator who finds it removing live bytes needs a
brake that is not a redeploy. It is the same shape `KDIVE_IMAGE_PUBLISH_GRACE_SECONDS` already has
for the sibling image sweep. One new `repair_kind` (`leaked_upload_objects`) joins `ALL_REPAIR_KINDS`
and so the ADR-0190 repairs counter; no new metric and no new report field. Not an AI surface.
