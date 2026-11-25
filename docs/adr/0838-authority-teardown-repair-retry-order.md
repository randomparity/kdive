# 0838 Authority teardown repair retry order

## Status

Accepted (2026-11-23)

## Context

The bounded preactivation-absence repair reads the oldest 100 receipts. A cleanup failure
or cleanup-required finalization retains its receipt without changing that ordering.
A real PostgreSQL reproduction with 100 failing cleanups left a healthy 101st receipt
unconsumed across both passes. Continuing inside one batch does not provide fairness
between batches. ADR-0623 and ADR-0754 still own receipt authenticity and consumption.

## Decision

Migration 0205 adds nullable `teardown_repair_last_retry_at` to authority System attempts.
Order the existing selector by `COALESCE(teardown_repair_last_retry_at, receipt_at)`, then
`receipt_at` and attempt ID. Preserve its eligibility predicates, role grants and 100-row cap.

After a cleanup/finalization exception or cleanup-required result, record the database's
current time through one reconciler-only function. It takes the selector's six identity
fields, acquires the System advisory lock and owner/attempt/job/System row locks in the
finalizer's order, and updates only a matching, still-eligible, unconsumed absence receipt.
It does not consume evidence, renew a lease or grant another worker attempt.

A marker failure is logged separately and does not abort later candidates already selected.
Cancellation propagates. No retry timestamp excludes a receipt from eligibility.

## Consequences

A failed page yields behind older untouched receipts even if the next pass runs after a
retry delay would have expired. Retained receipts stay eligible for subsequent attempts.
Existing receipt bytes, terminal outcomes and cleanup/ownership/lease fences are unchanged.
If retry writes remain unavailable, rotation cannot guarantee progress beyond that page;
healthy siblings inside the selected batch can still complete. Ordering uses the database
clock and does not introduce a scheduler or wall-clock deadline contract.

## Considered & rejected

- **Continue within the selected batch.** verified: the existing repair already continues
  after an exception; the real 101-receipt reproduction still returned zero twice.
- **Only exclude rows until a retry deadline.** judgment: a delayed reconciler can see the
  entire old page become due again. Last-attempt ordering retains progress across that delay.
- **Raise the batch cap or consume failed receipts.** judgment: these change bounded work or
  receipt ownership without fixing the selection mechanism.
