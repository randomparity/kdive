# 0865 — Concurrent provider diagnostics within the shared deadline

## Status

Accepted (2026-11-24)

Revises the serial provider execution choice in
[ADR-0854](0854-diagnostics-report-deadline-propagation.md), retaining its budgets,
categories, phase reservation and durable-job semantics.

## Context

The shared deadline prevents cumulative per-provider timeouts but serial dispatch
can spend it before a later healthy provider starts. The existing three-provider
proof at `19d225e16` expects `[1, 1, 0]` enqueues when the second provider stalls.

## Decision

The existing composite starts one local task per selected provider dispatcher,
passes their one absolute deadline, awaits them together, and returns results in
registration order. On unexpected failure or caller cancellation it cancels and
awaits its owned polling tasks, then re-raises the initiating exception unchanged.
No durable diagnostics job is canceled. Provider budgets and queue error mapping
stay in the existing dispatcher; server checks retain their existing phase.

## Consequences

A blocked provider cannot consume the worker phase before another provider starts.
Selected providers can compete for existing pool capacity; their queue waits remain
inside the same deadline. Output order and public request/response shapes stay stable.
Cooperative cancellation remains the bound; this provides no hard real-time guarantee.

## Considered & rejected

- **Keep serial execution.** verified: at `19d225e16`,
  `test_shared_phase_keeps_completed_results_and_does_not_start_expired_provider`
  explicitly pins third-provider starvation under the shared deadline.
- **Restart a budget per provider.** verified: ADR-0854 rejects cumulative budgets
  that exceed the report deadline.
- **Use TaskGroup.** judgment: explicit gather cleanup preserves the current direct
  unexpected-exception contract without adding an ExceptionGroup to that path.
- **Add a scheduler, semaphore or configurable concurrency.** judgment: one task per
  already-selected configured provider needs no additional policy or abstraction.
