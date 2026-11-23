# Durable operator backlog by dispatch lane

## Problem and authority

A real default-only worker claimed its install job and could not claim the remaining fenced
snapshot. The actual operator view, filtered to running jobs, returned queued/running totals
without a lane summary. The living queue selects a compatible addition to that existing read.
The user authorizes code quality fixes and internal interfaces while preserving public APIs.
ToolResponse.data is an open validated JSON mapping: adding a key preserves all existing fields
and input/envelope schemas. [ADR-0840](../../adr/0840-operator-queue-backlog-by-lane.md) records the
observation owner and scope. Existing role/audit/routing policy remains authoritative.

## Data flow and types

Beside queue_depth in jobs/queue.py define `QueuedLaneBacklog(TypedDict)` with required
`queued_count: int` and `oldest_queued_age_seconds: float | None`. Add
`queued_backlog_by_lane(conn: AsyncConnection) -> dict[str, QueuedLaneBacklog]`.
Initialize zero/null records for sorted unique lanes from
`dispatch_lane_for_kind(kind)` over `ACTIVE_JOB_KINDS`.

In one SELECT over jobs WHERE state='queued', group by dispatch_lane and return COUNT(*) plus
EXTRACT(EPOCH FROM statement_timestamp() - MIN(created_at)); order grouped lanes for stable
construction. Overlay every returned lane, including persisted lanes absent from active routing.
Convert count to int and nonempty age to float. Do not use claimability, current worker lanes,
project membership, per-page filters, retired-kind filtering, or transaction-start now().
Do not change queue_depth, routing, dequeue, timestamps, schema or worker configuration.

In ops/queue.py, call the helper on the existing connection after the existing operator gate
and input/cursor validation. Pass its result to _jobs_response through an explicit argument.
Add only data.queued_by_lane, with a two-field JSON projection that preserves known scalar
types; no Any, unchecked JSON cast or new serialization policy. Existing counts/items/limit/
truncated/next_cursor/suggested actions, per-job error envelopes and audit arguments stay intact.

## Agent-facing contract

The ops_jobs_list wrapper states that counts cover persisted queued rows across all projects,
irrespective of worker eligibility, states filter or pagination. Age is seconds since the oldest
queued row's current created_at, measured at database statement start; empty lanes have count0
and age null. Active routed lanes appear even when empty; other queued lanes may also appear.
These are observations, not a lease deadline, claimability measure or worker-coverage assertion.
Regenerate the tool reference and any required mirrors. Keep all request fields/parameters,
annotations, tool names and existing wrapper first-line summary unchanged.

## Failure model

- Actors/assets: only the existing platform operator can read these cross-project aggregates;
  preserve denial/read audit policy and keep payloads out of both aggregates and item projection.
- The helper is an internal read seam; the handler remains the authority boundary. Invalid
  parameters and denied callers do not trigger the new aggregate or receive its data.
- Database rows and kind-derived routing determine lanes; callers cannot supply a lane to
  enqueue. Persisted unrecognized lanes remain visible rather than being silently filtered.
- READ COMMITTED statements may observe concurrent changes independently. Counts and oldest age
  agree inside one aggregate, but no atomic snapshot of depth/backlog/page or age stability across
  pages is promised. Existing SQL failures propagate; no zero-data fallback hides failure.
- Database clock changes can affect measured age. It is an observation of the stored current
  creation timestamp, including recycle re-dating, not enforcement or a new scheduling policy.

## Validation

Promote the real unserved-lane reproduction into the operator tests using actual enqueue,
credential-bound default-only dequeue and operator handler. Also seed multiple queued rows in
one lane across projects with distinct database-relative created_at values, an old running row,
and an ineligible queued row. Verify full counts/oldest age even when viewing running jobs or
one page, empty zero/null lanes, denial without query/data, and JSON serialization.

Use direct helper tests for active-lane defaults and an unrecognized persisted queued lane.
Prove statement-clock age on a connection whose transaction began before a short database sleep;
measure bounds on the database clock so a transaction-start now() fault fails without host time.
Retain the existing state filters, pagination, error and audit suites. No new tests snapshot prose.

Controlled wrong-state scope, missing empty lane, transaction-clock and gate-order faults must
fail their named cases, then restore exact source bytes and rerun the original private reproduction
unchanged. Compare whole queue AST outside the new type/helper/import, and handler/renderer ASTs
modulo the explicit new read/argument/data projection and wrapper text. Public metadata must differ
only in this wrapper description; CLI/input/envelope schemas and all other tool records stay exact.
Run focused queue/operator/tool-doc tests, lint, whole-tree types, generated doc/resource checks,
build/wheel proof and scoped independent code/security review before resolving the item.
