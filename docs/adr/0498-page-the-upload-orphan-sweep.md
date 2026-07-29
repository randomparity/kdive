# ADR 0498 — Page the upload orphan sweep's listing and classify

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1569
- **Amends:** [ADR-0455](0455-upload-prefix-orphan-sweep.md) §3, whose cost paragraph disclosed the
  whole-root listing and whole-root array parameter as an accepted, unresolved cost and named this
  issue as its tracker; §6, whose budget now bounds the listing as well as the deletes; and §7,
  whose "one prefix-parameterised listing primitive" is now the paged iterator with the flat list as
  its delegate. ADR-0455's decisions — the three fences, the classify-then-re-check ordering,
  the per-root budget and its value, the threshold — are unchanged.
- **Depends on:** [ADR-0453](0453-row-first-upload-reap.md) §1 (the row-first reap whose partial
  failure produces the candidate set), [ADR-0496](0496-orphan-sweep-re-read-is-a-head.md) (the
  per-key re-read this change must not put a listing back into),
  [ADR-0092](0092-image-rootfs-lifecycle.md) (the `images/` sweep that keeps the flat listing).

## Context

`repair_leaked_upload_objects` listed each upload root in full — `store.list_prefix_with_mtime(root)`
returned every page flattened into one list — attributed the whole listing, and handed the entire
attributed set to `reclaimable_upload_keys` as four parallel array parameters in one statement. It
runs every `DEFAULT_INTERVAL` (30s) on the reconciler loop and again on every `ops.reconcile_now`.

The prefix it walks is unbounded. `repair_leaked_images`, whose shape ADR-0455 §1 reused, lists
`images/`, which is bounded by the image catalog. `local/runs/` is bounded by nothing: it accumulates
a vmcore per crashing run, a `capture_traffic` pcap per capture, and every chunked upload's
`<base>.partNNNN` parts, for the life of the deployment. At 100k objects that is a six-figure list of
`ObjectListing` tuples plus a multi-megabyte psycopg parameter payload, rebuilt every 30 seconds, in
steady state, with zero leak to reclaim. ADR-0455 §3 stated this plainly rather than arguing the
per-object comparison with `repair_leaked_images` carried, and filed it as #1569.

`MAX_RECLAIMS_PER_ROOT = 200` bounded neither half. It caps how many candidates reach the per-key
re-read and delete; the listing and the classify happened in full before the first candidate was
examined.

## Decision

### 1. The store's paged listing is the primitive; the flat list is its delegate

`ObjectStore.iter_prefix_pages_with_mtime(prefix)` yields one `list[ObjectListing]` per
`list_objects_v2` page, in store order, with `PageSize` pinned to a module constant rather than
inherited from boto3's default — the page is the bound a streaming caller relies on, so its size is
this module's property and not whatever boto3 defaults to next. `list_prefix_with_mtime` becomes the
one-line flattening delegate over it, so the pagination loop and its error mapping still exist once
(ADR-0455 §7's rule, now applied one level down). It keeps its remaining caller, `list_image_objects`
over the bounded `images/` prefix, where flattening is right and the image sweep classifies per
object anyway.

The sweep's **port** takes the paged method and not the flat one. `UploadOrphanStore` declares
`iter_prefix_pages_with_mtime`, so a port through which a whole root's listing could be handed to
this sweep no longer exists — the defect is unrepresentable at the seam rather than avoided by
convention at the call site.

An empty prefix yields one empty page, mirroring what `list_objects_v2` replies for a prefix
matching nothing. A caller counting pages must not read that as no request having been made, so the
iterator does not suppress it.

### 2. The sweep classifies and acts one page at a time, in store order

`_sweep_root` pulls a page, attributes it, classifies it in one statement, reclaims what that
statement approves, and only then pulls the next. Peak memory is one page of `ObjectListing` tuples
plus one page of `UploadOrphanCandidate`s; the classify's four arrays are a page wide. Both
acceptance criteria of #1569 are structural rather than tuned — there is no accumulator to grow.

**Order is preserved, and it is preserved the same way it was.** Pages arrive in store order (S3
lists lexicographically; boto3's paginator does not reorder), and within a page the deletes follow
the *page's* order filtered by the classify's approved set — not the classify's row order, because a
planner may reorder an anti-join's output and the store may not. So the deletes come out in the order the
flattened listing produced them. That matters for the reason ADR-0455 §3 gives: a pass truncated by
the budget or by a fault is only reproducible if the prefix of the sequence it got through is
deterministic. (The *membership* of that sequence can differ slightly from the unpaged sweep's,
because each page's classify reads its own `now()`; §Consequences states the bound.)

The `_warn_if_wholly_unattributable` drift check (ADR-0455 §4) accumulates its `listed`/`attributed`
counts across the root's pages and reports once after the root, rather than per page. Its subject is
a *root* whose key layout the parser no longer recognizes; a per-page warning would fire whenever a
page boundary happened to isolate unattributable keys, which is a listing artifact and not drift.

### 3. A mid-root listing fault ends that root and keeps the pages already swept

With the whole-root listing there was no partial state: the listing either produced a candidate set
or produced nothing, so ADR-0455 §5's skip-and-count cost the entire root. Paged, the fault can
arrive at page 2 after page 1 has already deleted irreversibly, and unwinding is not available — the
objects are gone. The root is therefore abandoned from the failed page on, the fault is counted, and
the pass raises once at the end, exactly as §5 chose; what is new is only that the skip can follow
work already done.

That is strictly more progress than before, and it is safe for the same reason a budget-truncated
root is: this sweep commits nothing, so the next pass re-derives the identical candidates from the
page this one stopped at. The alternative — discarding a page's deletes on a later page's fault —
is not expressible, and treating the fault as fatal to the pass would restore precisely the
starvation of `local/investigations/` that §5 and §6 exist to prevent.

### 4. `MAX_RECLAIMS_PER_ROOT` now also stops the listing; what it counts is unchanged

The budget still charges one unit per candidate that reaches the per-key re-read, per root, whatever
that candidate's outcome — a declined re-check and a failed delete cost the same HEAD and query as a
successful one (ADR-0455 §6). Nothing about the accounting moves.

What changes is that a spent budget now also stops paging: `_sweep_root`'s loop is conditioned on the
budget, so a drain examines its allowance out of the first page or two and never enumerates the
backlog behind it. That is the same brake reaching one term further, and it is the reason the drain
case improves and not just the steady-state one.

One log line is now slightly less precise, and the wording says so. The budget message used to be
emitted only when a 201st reclaimable candidate actually existed, because the whole root's
candidates were in hand. Paged, reaching the allowance is known but the existence of a remainder is
not — establishing it would cost a LIST round trip for a log line. So the message fires whenever the
allowance is reached and reads "any remaining backlog", not "the remaining backlog". A root holding
exactly the allowance's worth of orphans now logs it once with no remainder to reclaim. That is an
INFO line about a brake that did engage; buying precision with a round trip is the wrong trade.

### 5. The narrower classify is index-served, and this was measured rather than reasoned

The concern paging invites is the planner choosing worse at the narrower width, turning one indexed
anti-join into N sequential scans of `artifacts`. It does not, and the first guess about why was
wrong and is recorded so it is not re-derived: `unnest`'s row estimate **does** track the parameter
array's real length, so the width is visible to the planner and the plan genuinely does change with
it.

It changes toward the index. Measured against a `migrate`d schema with #1570's `artifacts
(object_key)` btree (migration `0081`) and 200k `artifacts` rows:

| driving width | plan |
| --- | --- |
| 1000 (one page) | `Nested Loop Anti Join` → `Index Only Scan using artifacts_object_key_idx` |
| 20000, 100000 (a root) | `Hash Anti Join` → `Seq Scan on artifacts` |

The page-wide statement is the index-served one; the root-wide statement was the sequential scan. The
crossover sits right at 80k rows on this schema — close enough that a byte of row width flips it —
which is why the test pinning this seeds 200k rather than sitting on the boundary.

Wall-clock time is flat. Classifying 100k candidates against 200k `artifacts` rows took 1.89s in 100
page-wide statements, 1.86s in 20, 1.87s in 5, and 1.91s in one root-wide statement. The planner's
cost units differ by more than an order of magnitude across those shapes; the elapsed time does not,
because the dominant term is parameter transfer and `unnest` materialization rather than the join.
So paging is not bought with classify time, and the honest claim is "no measurable change", not "a
speed-up".

### 6. The per-key re-read is untouched

ADR-0496 made the per-key mtime re-read a single `head`, and this change does not put a listing back
into it. `_reread_from_store` is unmodified: one `head_object`, returning the candidate with a
refreshed `last_modified` and the `etag` observed in the same round trip. The paged listing is the
sweep's *enumeration*; the re-read remains its *stat*, and the two stay separate methods on the port
for the reason ADR-0496 §2 gives.

## Consequences

**The acceptance criteria are structural, not tuned.** Peak memory and per-statement parameter width
are bounded by `_LIST_PAGE_SIZE` because there is nowhere for a root's listing to accumulate — not
because a limit is checked. The fences, the threshold, and the re-check ordering are untouched, and
the delete **order** is unchanged: store order, taken from the page and never from the anti-join's
rows.

**The reclaimed *set* is not bit-identical to the unpaged sweep's, and the reason is `now()`.** Each
page's classify opens its own short transaction (it always did — §Decision 2 of ADR-0455 keeps the
snapshot off the blocking store calls), so `now()` advances between pages. The unpaged sweep aged
every key in a root against one `now()`; the paged sweep ages page 20's keys against a `now()`
however long the first nineteen pages took. An object sitting at `grace - 30s` when page 1 was
classified can therefore cross the threshold by page 20 and be reclaimed in the same pass that would
previously have spared it.

This is stated rather than claimed away, and it is not a correctness problem in either direction. The
shortening is bounded by one pass's own duration, against a threshold of `orphan_grace + upload_ttl`
— 48h at the defaults — so the object was reclaimable within seconds of when it was reclaimed, and
it had been rowless, manifest-less and unwritten for two days. Nothing becomes reclaimable that was
not about to be. The opposite direction cannot happen at all: `now()` only advances, so no key is
protected by paging that the unpaged sweep would have deleted. What would have been a real problem is
a *shared* `now()` captured once per pass, because that would age a root's tail against a clock from
before its own listing; per-page `now()` is the conservative choice and also the one the code already
made.

**A root is no longer atomic with respect to a listing fault.** §3 makes this explicit because it is
the one behavioural change an operator could observe: a pass that previously logged "could not list
`local/runs/`" and deleted nothing under it may now log the same thing having deleted a page's worth.
The counts in both the abort log and the raise cover it, so nothing is silent.

**Two round-trip counts move in opposite directions, and neither is a regression.** Steady state with
no leak issues the same LISTs it always did (a root must be enumerated to find nothing) but as
`ceil(N/1000)` classify statements instead of one — measured flat, §5. A drain issues *fewer* LISTs
than before, because the budget stops the paging. The per-key HEAD and delete counts are unchanged.

**`list_prefix_with_mtime` keeps exactly one caller.** `list_image_objects`. It is not dead and it is
not deprecated: `images/` is bounded by the image catalog, the image sweep classifies per object, and
ADR-0496 §2 already recorded that its unbounded pagination is right for its remaining callers. A
future sweep over an unbounded prefix should take the iterator, and the port-level split in §1 is
what makes that the path of least resistance.

**What this does not address.** The repair seam still holds one of the `max_size=10` pool's slots
checked out for the whole sweep, which is #1554's to restructure. ADR-0455 §3's re-read→delete
residual (#1574, mitigated by [ADR-0497](0497-finalize-verifies-its-object-before-committing-rows.md))
is unchanged, as is ADR-0453's second residual (#1557). The detection delay the threshold imposes is
unchanged.

No schema, no migration, no config, no MCP or RBAC surface, no new setting. `MAX_RECLAIMS_PER_ROOT`
is not re-tuned.
