# 0854 — Diagnostics report deadline propagation

## Status

Accepted (2026-11-23)

Builds on [ADR-0164](0164-diagnostics-worker-vantage-dispatch.md), retaining its reserved
worker-phase requirement, and [ADR-0091](0091-doctor-diagnostics-model.md).

## Context

The service currently applies its overall deadline only to server checks. Worker dispatch
then starts separate per-provider budgets, excludes enqueue time and can wait indefinitely
for queue I/O. A multi-provider report can exceed its advertised orchestration budget.

## Decision

The service owns one event-loop monotonic report deadline. With dispatch enabled it reserves
`min(15 seconds, max(0, overall_timeout / 2))` for the worker phase and limits server checks to
the remainder. The worker phase ends at the earlier of the report deadline and its reserved
duration measured from worker-phase entry. Providers share that same deadline in existing order.

The internal dispatcher port accepts an optional keyword deadline in event-loop seconds.
Each job dispatcher bounds its entire enqueue/poll/sleep operation with `asyncio.timeout_at`,
using the earlier of the propagated deadline and its configured local budget. An already
exhausted budget returns existing attributed worker-unavailable errors without enqueuing.
The injected elapsed clock remains a local polling seam, starting before enqueue; it is never
compared to an absolute event-loop deadline. Standalone zero-argument calls remain supported.

Caller cancellation propagates. Local timer expiry returns existing worker-unavailable
results; independent queue exceptions retain their existing infrastructure-error mapping.
Completed sibling verdicts remain in the composite's accumulated results. Timing out the
report does not cancel a durable diagnostics job or assert that its worker has stopped.

## Consequences

With worker dispatch enabled, default production orchestration allocates at most 15 seconds
to each phase within its
30-second report budget. With no overall limit, per-check and per-provider local bounds remain.
The public service constructor signature, MCP parameters, result schema and authorization stay
unchanged. Injected dispatcher implementations must accept the optional deadline keyword; this
Python port is internal under [ADR-0041](0041-versioning-release-process.md) decision 1.
Timeouts rely on cooperative async cancellation; this adds no hard real-time guarantee.

## Considered & rejected

- **Pass only time left after unrestricted server checks.** verified: ADR-0164 decision 4
  reserves worker time specifically to prevent slow server checks consuming its opportunity.
- **Restart a budget for each provider.** verified: service.py's composite currently runs
  providers serially, allowing their individual budgets to accumulate beyond the report limit.
- **Add a global cancellation framework or parallel dispatch.** judgment: existing explicit
  service/dispatcher boundaries and standard-library timeouts suffice for this repair.
