# 0840 Operator queue backlog by lane

## Status

Proposed

## Context

Worker queue metrics sample the lanes that worker polls. A lane with no accepting worker
therefore has no worker sample. The existing operator view reports total depth by state,
while its bounded, filterable job page cannot summarize every lane across the whole queue.
A real queue reproduction left a snapshot queued after a default-only worker claimed its
install job; the operator's running-filtered response showed a queued total without a lane.

ADR-0062 owns cross-project operator inspection and its read audit. ADR-0550 owns kind-derived
routing and worker lane claims. Neither contract needs a new worker registry or admission policy.

## Decision

Add `data.queued_by_lane` to the existing `ops.jobs_list` response. Read it from one database
aggregate over persisted `queued` rows, independent of workers, eligibility, filters and page.
Each lane contains integer `queued_count` and `oldest_queued_age_seconds`: seconds from the
oldest queued row's current `created_at` to database statement start, or null when empty.
Seed every lane routed by active job kinds with zero/null; retain other persisted queued lanes.

The queue helper returns a precise two-field typed dictionary; the existing operator renderer
projects it to JSON. Keep the platform-operator gate, read audit, existing response fields,
request and envelope schemas, tool annotations and pagination unchanged. The wrapper documents
the added data contract, and its generated reference follows it.

## Consequences

An operator can inspect backlog for an unserved lane without relying on worker metrics or
walking every page. A queued total does not prove that a worker is serving that lane or that
the jobs are currently claimable. Recycling already re-dates `created_at` under ADR-0447.
Each aggregate is consistent within its own statement; depth, backlog and page queries retain
the existing READ COMMITTED observation model. This adds no whole-response snapshot guarantee.

## Considered & rejected

- **Sum worker measurements.** verified: `count_claimable` receives only a worker's accepted
  lanes; no worker polling a lane means no sample from that path.
- **Derive totals from the returned page.** verified: `ops.jobs_list` filters and caps rows,
  while its current state-depth query deliberately spans the whole queue.
- **Add fleet coverage admission or a cached summary table.** judgment: worker orchestration
  and a second state owner exceed the passive inspection gap and are unnecessary here.
