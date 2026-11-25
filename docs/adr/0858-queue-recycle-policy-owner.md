# 0858 — Keep job recycle policy in the durable queue

## Status

Accepted (2026-11-24)

## Context

The queue implements terminal recycle policy, while MCP replay lookup and lifecycle steps
repeat its terminal-state sets. These definitions currently agree.

## Decision

Make `jobs/queue.py` the sole terminal eligibility owner. Its map serves SQL parameters
and transport-neutral replay lookup. Keep clock-dependent running eligibility exclusively
in the existing SQL predicate; reject predictive lookup for that policy before DB access.
Expose the same helper through MCP's existing import surface without a forwarding wrapper.
Steps use the existing actual admission result for their replay marker.

## Consequences

Policy edits have one owner. Existing callers preserve pre-admission ordering. Predictive
lookup remains an observation, not a concurrency guarantee; actual admission is authoritative.
SQL, locks, ledger policy, audit, attempt fencing and public response semantics stay unchanged.

## Considered & rejected

- **Keep coordinated copies.** judgment: three policy representations obscure ownership and
  require coordinated maintenance despite the queue already owning the state transition.
- **Add another policy service.** judgment: the existing queue owns admission and lookup;
  another module adds a dependency without a separate responsibility.
- **Rewrite every caller import.** judgment: retaining one canonical convenience export avoids
  unrelated call-site churn without retaining a second implementation.
- **Predict expired running leases in Python.** verified: at commit 5ec3694e7c92,
  `enqueue_with_status` evaluates `lease_expires_at < clock_timestamp()` in its UPDATE;
  an earlier client-side clock cannot establish that later database-clock condition.
