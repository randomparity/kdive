# ADR 0503 — A malformed store reply is converted at the store boundary, not at its caller

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1685
- **Amends:** [ADR-0496](0496-orphan-sweep-re-read-is-a-head.md) §2, which argued that
  `HeadResult.last_modified` needs no `None` arm because `HeadObject` always returns
  `Last-Modified`. That argument is upheld — the field stays required — but its unstated
  consequence, that the read is therefore an unguarded subscript, is what this ADR closes. §2's
  disclosure of the unguarded read as a residual is now discharged; nothing else in ADR-0496
  changes.
- **Depends on:** [ADR-0455](0455-upload-prefix-orphan-sweep.md) §5 (the skip-and-count fault
  handling whose error class this converts into), [ADR-0498](0498-page-the-upload-orphan-sweep.md)
  §3 (the paged listing that makes a per-entry fault reachable mid-root).

## Context

`ObjectStore` reads fields out of a **successful** store reply by subscript:
`resp["LastModified"]`, `resp["ETag"]`, `int(resp["ContentLength"])` in `head`, and
`obj["Key"]` / `obj["LastModified"]` for each listing entry. On a store that implements the S3 API
those fields are always present — boto3 parses the reply against the service model — so the
subscripts were never wrong about the contract. They were wrong about what happens when the
contract is broken: a 200 whose body is missing a field, or carries it as some other type, raises a
bare `KeyError` (or, past the subscript, an `AttributeError` out of `_normalize_etag`).

That is not the error class this module's callers handle, and one caller makes the difference
severe. Since ADR-0496 the upload orphan sweep's per-key mtime re-read is `store.head`, reached
from `_delete_if_still_reclaimable`. `_reclaim_page` catches `CategorizedError` for a per-key fault
and `psycopg.Error` for the classify; a `KeyError` matches neither, so it escapes `_reclaim_page`,
escapes `_sweep_root`, and ends the pass. One unreadable reply costs every remaining candidate under
that root *and* the sibling root — while the sweep exists precisely to drain a backlog that only
grows when a pass does not finish. The listing path is worse still: a malformed entry escapes
`_next_page_or_fault`, so it ends the pass before `local/investigations/` is listed at all.

## Decision

### 1. The conversion happens at the boundary that produced the reply

A `_StoreReply` binds one successful response to the identity of the request that produced it — the
operation, the bucket, and the object key (or, for a listing entry, the prefix that listed it, since
an entry missing its `Key` has no key to be named by). Its `required(field, expected)` reader raises
a `CategorizedError` with `ErrorCategory.INFRASTRUCTURE_FAILURE` when the field is absent or is not
an `expected`, naming the store call, the bucket, the subject, and the field.

Naming the field is the actionable part. "object-store head_object for 'k' failed" leaves an
operator watching one key fail every pass forever with no way to tell a malformed reply from a
per-key deny; "omitted the required 'LastModified'" points at the endpoint.

### 2. The type is checked, not coerced

`required` takes the type the API promises and rejects anything else, rather than coercing. The
`int(resp["ContentLength"])` coercion is dropped as redundant — a validated `int` needs none — and
the reason to prefer the check is that a coercion turns a nonsense value into a plausible one. An
ill-typed field is also the half a presence check misses, and it is not a hypothetical: a `str`
where `LastModified` belongs does not raise at the subscript at all. It reaches `_RECLAIMABLE_SQL`
as a `text` in a `timestamptz[]` parameter position, which lands in `_reclaim_page`'s
`psycopg.Error` arm and ends the **root** — a quieter version of the same defect, and one that
looks like a database fault.

### 3. The policy is decided for every field a read returns, once

All three of `head`'s required fields go through `required`, not just the one #1575 made
load-bearing, and so do both of a paged listing entry's. `list_prefix` requires `Key` and nothing
else, because `Key` is all it returns; the contract is per read, not one field set for the module.
Guarding only the field a caller had happened to reach would leave the identical defect reproducible
through `ETag` and `ContentLength`, which is how this one arrived.

The line is drawn at the **reads**. The write and multipart calls subscript their replies too
(`put_object`'s `ETag`, `create_multipart_upload`'s `UploadId`, `upload_part_copy`,
`complete_multipart_upload`), and they are deliberately left alone: each is one request's own
result with no per-item fault handler above it, so a malformed reply there already fails exactly the
one operation it belongs to and fails it loudly. Converting them would change tested error surfaces
for no reachable defect. The reads are different because their callers are sweeps that classify
per item and are required to survive one bad item.

### 4. Rejected: widen the sweep's `except` to catch `KeyError`

This is the smaller diff and it is wrong. `_reclaim_page`'s docstring states the constraint
directly — "Only the database's own error class is caught for the classify — a bug in this module
should still abort the pass" — and `KeyError` is a shape a real bug in the sweep takes. Adding it
would convert every such bug into a silently counted per-key skip, so the sweep would keep
reporting a healthy-looking tally while doing nothing, which is the failure mode ADR-0455 §4's
drift warning already exists to catch by another route. The boundary conversion gets the resilience
without spending the loudness: after it, a `CategorizedError` means the store failed and anything
else still means this module did.

Rejected for a second reason: the sweep is not the only caller of these reads. A fix at its
`except` leaves every other caller — the `images/` sweeps through `list_prefix_with_mtime`, the
finalize verification's `head` — facing the original `KeyError`.

## Consequences

A malformed reply now costs the orphan sweep one key on the `head` path and one root on the listing
path, in both cases through the skip-and-count path ADR-0455 §5 already specifies, with the pass
raising once at the end. Nothing about *what* the sweep deletes changes, and no caller's `except`
was widened: the property that a genuine bug in `upload_orphans` aborts the pass is preserved and is
now pinned by a test that fails if someone widens it later.

`head` will now reject a reply it previously accepted: one whose `ContentLength` is a numeric
*string*. No S3-compatible store produces that — boto3 types the field as a `long` — and the
alternative was keeping a coercion whose only effect is to accept a reply the API cannot send.

Tests are added at both layers, because the defect lives in the seam between them. The store tests
assert the category, the message, and the details for each field missing and each field ill-typed,
plus the boundary itself — an absent `ChecksumSHA256` or `Metadata` must still yield a `HeadResult`,
which is the over-rejection a required-everything check would cause. The sweep tests drive the
**real** `ObjectStore` over a fake boto client rather than the suite's fake store, because a fake
store raising `CategorizedError` directly would assume away the thing under test; the malformed key
is seeded to sort first so that the load-bearing assertion is that the keys *behind* it were still
reclaimed. Against the unguarded code that assertion observes zero deletes.

No schema, no migration, no config setting, no MCP tool-surface or RBAC change.
