# 0846 — Count unconsumed authority receipts consistently

## Status

Proposed

## Context

Worker claiming excludes a job with a terminal, unconsumed authority System receipt.
Claimable-depth sampling lacks that exclusion. On fully migrated PostgreSQL17, one accepted
lane reports depth1 while the same worker cannot claim its expired job. Receipt consumption
belongs to authority repair, not telemetry.

## Decision

Add forward migration0206 to the existing depth function. Reuse the claim-side correlated
`NOT EXISTS` condition for terminal receipts whose `consumed_at` is null. Retain all existing
validation, clocks, capture gates, retry-proof eligibility, security attributes and grants.
Guard the expected function source before replacing it. Do not modify applied migrations or
claiming. No new query helper, privilege, table or public interface is needed.

## Consequences

Depth reflects the existing receipt handoff gate without consuming it. Counting remains an
observation, not a reservation; concurrent changes can still affect a later claim. The existing
two query bodies and their clock choices remain explicit residuals. This change fixes verified
receipt parity and makes no claim of complete policy unification.

## Considered & rejected

- **Keep the mismatch.** verified: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest
  .desloppify/repro_claim_depth_parity.py -q --tb=short` (one command), on9cd0695e's unchanged
  0205 schema, fails with depth1 and claim=[] in5.82s. Permanent receipt-parity tests retain
  that observable contract.
- **Rewrite claiming or add a shared predicate now.** judgment: the current execution step
  requires the missing aggregate exclusion. Claim ownership, locks and retry-proof writes need
  no change. A broader query interface warrants separate measured plan and concurrency evidence.
