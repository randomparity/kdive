# 0852 — Bounded System repair lanes advance independently

## Status

Accepted (2026-11-23)

## Context

Leaked-mutation and stalled-reprovision repairs each select the first 100 eligible System
IDs. Candidate failures leave those rows eligible. Holding their first 100 System locks
against real Postgres produced `[0, 0]` repairs over two runner passes in each lane;
a healthy 101st System was never inspected. The stalled-teardown repair already avoids
this starvation with a runner-owned keyset hint ([ADR-0842](0842-stalled-teardown-repair-keyset-progress.md)).

## Decision

Extend the existing progress object to hold independent leaked-mutation and
stalled-reprovision UUID hints. Name the expanded type `SystemRepairProgress`; retain
`StalledTeardownProgress` as a direct identity alias, its historical `after` teardown field,
and the existing `stalled_teardown_progress` standalone keyword. Production uses the
broader type. One runner owns the object; configuration does not own progress.

For each affected lane, select at most 100 IDs strictly after its nullable hint.
An empty tail permits one wrapped lookup. Process only one nonempty page; reset that
lane after a successful all-empty lookup. Failed selection leaves progress unchanged.
UUID zero remains a real value rather than a sentinel.

Extract the existing locked candidate transaction into a private helper. Advance the
lane hint after ordinary completion, including guarded skips and handled candidate
exceptions. Cancellation leaves the interrupted candidate for a later attempt. Preserve
transaction framing, System advisory locks, eligibility rechecks, counts, category/audit
writes, activation exclusions and mutation-discharge authority. Retry logs say later pass.

## Consequences

For a stable finite eligible population, persistent earlier failures no longer prevent
later inspection through serialized runner passes. Hints are scheduling observations,
not authorization, durable evidence or a wall-clock guarantee. Restarts reset them;
new lower IDs wait for wrap. Concurrent manual passes are not serialized by the hints;
existing database locks and guarded writes remain the safety owners. No migration is needed.

## Considered & rejected

- **Raise the limit.** verified: the two queries always select a bounded ordered prefix;
  any fixed larger prefix can still be filled by eligible failures.
- **Checkpoint before candidate processing.** verified: the lock acquisition awaits;
  cancellation there would postpone an uninspected candidate.
- **Persist a scheduling table.** judgment: existing runner-owned observations provide
  the required finite-population progress without a second durable owner.
- **Remove the historical progress API.** judgment: a direct class alias preserves
  the existing import and constructor without introducing another implementation.
