# ADR 0496 — The upload orphan sweep's per-key re-read is a `head`, not a prefix `LIST`

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1575
- **Amends:** [ADR-0455](0455-upload-prefix-orphan-sweep.md) §3, which priced the per-key mtime
  re-read as one round trip and then disclosed, in the same paragraph, that it is not one for a
  chunked base key; and §6, whose per-candidate cost model has the same LIST term. That
  disclosure is now discharged; ADR-0455's decisions — the three fences, the
  bulk-classify-then-re-check ordering, the per-root budget and its value — are unchanged.
- **Depends on:** [ADR-0104](0104-chunked-external-upload-reassembly.md) §1 (the
  `<base>.partNNNN` key shape that makes a base key's LIST unbounded),
  [ADR-0453](0453-row-first-upload-reap.md) §1 (the row-first reap whose partial failure produces
  the base-plus-parts candidate set).

## Context

`_with_current_mtime` re-read a candidate's store mtime with
`store.list_prefix_with_mtime(candidate.key)` and then filtered the result for an exact key
match. The filter made the read *correct* — a LIST on a full key also returns every key that key
prefixes, and only the candidate's own mtime may decide its fate — but it did nothing about the
cost, because `ObjectStore.list_prefix_with_mtime` paginates `list_objects_v2` to exhaustion with
no `MaxKeys` bound.

The key shape that makes this expensive is the one the sweep exists to drain. A chunked upload's
parts are keyed `<base>.partNNNN` (ADR-0104 §1), and a row-first reap (ADR-0453 §1) that fails
partway through a chunked window leaves the base key and every one of its parts rowless
*together*. So the sweep's own subject matter is precisely the candidate set where re-reading the
base key enumerates `1 + N` keys at 1000 per round trip to retrieve a single `LastModified`. Part
keys prefix nothing and cost one round trip each, so the amplification is per base key, bounded
per pass by `MAX_RECLAIMS_PER_ROOT` candidates per root.

`ObjectStore.head` already existed and already issued exactly one `head_object`, returning a
normalized `etag`. What it did not return was the object's mtime, so it could not back this
re-read as it stood.

## Decision

### 1. `HeadResult` carries `last_modified`, and the re-read is `store.head`

`HeadResult` gains a `last_modified: datetime`, populated from `head_object`'s `LastModified`.
`_with_current_mtime` becomes `_reread_from_store` and issues one `head` per candidate. The
`UploadOrphanStore` port declares both reads, because they answer different questions: the sweep
needs *every* key under a root once per pass (`list_prefix_with_mtime`), and *one* key's current
mtime immediately before each delete (`head`). Serving the second with the first was the defect.

The exactness property that the discarded filter provided is now structural rather than
enforced: a `head` on a key resolves to that key or to nothing, so no sibling can lend it an
mtime. `list_prefix_with_mtime` keeps its unbounded pagination, which is right for its remaining
caller — a root listing genuinely wants every key.

> **Amended by [ADR-0498](0498-page-the-upload-orphan-sweep.md) (#1569).** The enumeration half of the
> port is now `iter_prefix_pages_with_mtime`, which streams a root a page at a time;
> `list_prefix_with_mtime` is no longer on this port at all and its remaining caller is the bounded
> `images/` sweep. The split this section defends is unchanged and is now enforced by the port rather
> than by convention — the sweep cannot ask for a whole root, and the re-read is still one `head`.
> "A root listing genuinely wants every key" stays true; it wants them a page at a time.

### 2. `last_modified` is required, not optional

`HeadObject` always returns `Last-Modified`; there is no S3-compatible response that omits it. A
`datetime | None` would therefore have added a branch no real store can take, at the one call
site whose whole job is to decide an object's age — and a fail-closed "no mtime, decline" arm
reads as a fence when it is dead code, while a fail-open one deletes live bytes. The cost of
making it required is that every construction site supplies it: one in `src/` and ~70 across the
test fakes, which share the fixed `tests.clock.STORE_MTIME` when the mtime is not what the test
is about. That is mechanical churn, and it is bought by a type that cannot express an object
without a modification time.

### 3. Rejected: `max_keys=1` on `list_prefix_with_mtime`

The issue offered this as the alternative, and it does fix the round-trip count: S3 returns keys
in lexicographic order and a key sorts strictly before every key it prefixes, so a one-key page
of `Prefix=<full key>` is both exact and a single request. It is rejected on two counts.

First, the exactness is *derived* rather than structural — it holds because of an ordering
guarantee and a prefix-sorting argument, so the exact-match filter has to stay, and the next
reader has to re-derive why a LIST is an acceptable stat. A `head` on a key is a stat.

Second, and decisively for what comes next, `list_objects_v2` gives no way to observe the object
identity the caller acted on in the same read that decided it. `head_object` does.

### 4. The re-read stays immediately before the delete, and now yields an identity

The ordering `re-read → re-check → delete` is unchanged, and deliberately so: ADR-0455 §3
discloses a residual — a PUT landing between the re-read and the `delete_object` is destroyed —
and the tightness of that gap is the only thing currently bounding it. Hoisting, caching, or
batching the re-read to amortize it would widen exactly the window #1574 exists to close, so the
cheaper read is spent on being cheap, not on being moved.

`_reread_from_store` returns a `_CurrentObject` pairing the refreshed candidate with the `etag`
observed in the same `head`. The two cannot disagree about which bytes were examined, because
they came back in one response. The reclaim log line names the etag, so an operator reading a
delete can tell which version of a re-PUT key went — and the identity is in scope at the delete
site for a conditional delete to fence on, which is the shape #1574 needs. **This ADR does not
close that race**; it makes closing it a change to the delete call rather than a change to the
read that precedes it.

## Consequences

The per-key re-read is O(1) round trips for every key shape, so ADR-0455 §6's per-candidate cost
model — "a LIST and a query" — is now "a HEAD and a query", and the LIST term is gone rather than
bounded. The remaining per-key cost is the `artifacts` anti-join, which is an index scan since
#1570 added the `artifacts (object_key)` btree in migration `0081`, and the module comment on
`MAX_RECLAIMS_PER_ROOT` is corrected to say so — it still called that query unindexed.

`MAX_RECLAIMS_PER_ROOT` is **not** raised here. The budget bounds the reconciler's sequential
catalog, and one of its two per-candidate terms is unchanged; re-tuning it wants a measurement
against a real backlog, not an inference from a removed term.

The S3 action changes for this call — `list_objects_v2` needs `s3:ListBucket`, `head_object`
needs `s3:GetObject` — and this is the honest version of what that costs. The reconciler already
issues `head_object` through the *same* `ObjectStore` instance (`processes/reconciler.py` passes
one object as both `upload_store` and `image_store`, and `reconciler/cleanup/images.py`'s
dangling sweep calls `head_present`), so no *new* grant is introduced. But that sweep HEADs only
when it has dangling candidates, so a credential holding `ListBucket` + `DeleteObject` and not
`GetObject` could have been latently broken and never shown it; after this change the orphan
sweep exercises the grant on every candidate. The failure is loud and safe — the re-read is a
per-key failure site, so such a key is skipped, counted, and the pass raises rather than deleting
on a re-read it could not make — and it is pinned by a test that fails `head` for one key and
asserts the key behind it is still reclaimed.

The sweep is the only consumer of the new field, so every other `HeadResult` caller now carries a
value it ignores. That is the honest price of the required-field decision in §2 and is recorded
rather than left to be discovered.

Nothing about what the sweep *deletes* changes: the fences, the threshold, the per-root budget,
the skip-and-count fault handling, and the listing-order traversal are all untouched, and the
whole existing sweep suite passes unmodified — reverting `_reread_from_store` to the LIST-and-
filter implementation fails exactly one test, the new cost one, and no delete-behaviour test
changed meaning.

Three tests are added or corrected rather than one. The cost test asserts one `head` per examined
candidate **and** no listing beyond the one per root, because a call-count assertion alone would
also have passed for a bounded LIST. A re-read-failure test covers the fourth per-key failure
site, which §2's action change makes newly worth pinning. And the pre-existing test named for
"the re-read takes the exact key, not a sibling it prefixes" is renamed and re-documented: it
never pinned the re-read — deleting the re-read outright leaves it green, because the young part
it seeds is excluded by the *bulk classify* and never reaches the re-read at all — and what it
actually pins, that `_RECLAIMABLE_SQL` ages each candidate on its own mtime, is worth keeping
under an honest name. The property it was named for is not re-pinned elsewhere because it is no
longer a property that can fail: a `head` on a key resolves to that key or to nothing.

No schema, no migration, no config setting, no MCP tool-surface or RBAC change.
