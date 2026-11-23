# 0839 External-boot repair keyset progress

## Status

Accepted (2026-11-23)

## Context

All four external-boot repair lanes select the first 100 eligible activation IDs. A skipped
source, live successor or failed enqueue leaves its activation eligible, so the next pass
can inspect the same page forever. A real Reconciler reproduction with 101 prepared rows
never reached the last row in two passes; making the first page ineligible let it enqueue.
This differs from receipt retry ordering, whose durable SQL owner is ADR-0838.

## Decision

Give each Reconciler its own typed mapping from repair lane to last inspected activation UUID.
Pass that map explicitly through the existing repair catalog and lane call. Standalone callers
can reuse an explicit map; an omitted map means one-shot work. Do not put hints in shared config
or globals and do not cache the repair plan.

Wrap the existing eligibility query in a keyset filter and keep the 100-candidate limit.
A missing hint includes every eligible UUID, including zero. On an empty page after a hint,
read once from the beginning in the same pass. At most one nonempty page is inspected.
Advance after each ordinary skip, success or caught candidate failure. A cancellation leaves
the interrupted candidate unadvanced; page selection or job-lookup failure also retains the hint.

The cursor is a progress hint, not durable authority. Existing source, live-successor, provider,
marker, transaction and deduplication guards remain the safety owners.

## Consequences

A stable reconciler reaches later rows over repeated passes and revisits earlier rows after
wrapping. Separate lanes and reconciler instances keep independent progress. Restart starts at
the beginning; repeatedly restarting before a second pass can still delay later work.
Manual overlapping passes may revisit rows; durable queue guards still govern insertion.
There is no persisted cursor, new scheduling service, candidate cap increase or wall-clock SLA.

## Considered & rejected

- **Only continue inside the selected page.** verified: the existing loop already does this;
  the real Reconciler reproduction still enqueues zero twice.
- **Store hints on shared config or in the catalog closure.** judgment: multiple instances may
  share config or the module catalog; progress belongs to the running instance.
- **Change every durable eligibility query or cache the whole plan.** judgment: an outer keyset
  filter preserves the current predicates, and explicit state extends the existing factory seam
  without changing other repair callback lifetimes.
