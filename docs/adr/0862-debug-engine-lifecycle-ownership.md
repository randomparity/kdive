# 0862 — Keep debug lifecycle bookkeeping with its engine

## Status

Accepted (2026-11-24)

## Context

DebugEngineRuntime owns attachment and pending-root registries, admission and active tasks.
DebugRuntimeResolver currently interprets their private representation while selecting providers,
sweeping and shutting down. Existing lifetime proofs require draining unpublished first attaches
before final cleanup; moving responsibilities must retain that behavior.

## Decision

Expose session snapshot, per-engine sweep, begin-close and async drain on the existing engine.
Resolver delegates those policies and retains provider cache/enumeration and periodic scheduling.
Close all engines before awaiting periodic stop/drain; final sweep follows all admitted work.
Preserve current locks, DB eligibility/timeout, retry, cancellation and error-retrieval policies.
Keep existing reap/lock_for callers and app lifespan integration; remove resolver private access.

## Consequences

Engine-private representation can change without resolver edits. Lifecycle responsibility stays
with existing resource ownership, with no new module or protocol. Cleanup remains best-effort
and pending owners remain retryable. This adds no public, persisted or concurrency-policy change;
process-scoped lazy attach remains as [ADR0034](0034-debug-plane-gdbmi-tier.md) specifies.

## Considered & rejected

- **Keep resolver bookkeeping.** verified: source inspection at bf7f10238c74 shows resolver owned,
  sweep_once and lifespan read/write the engine registry, pending roots, closing flag and active
  task set. Native151 observation independently confirms this coupling. Existing tests protect
  runtime behavior but do not establish the missing engine boundary.
- **Introduce a lifecycle manager or shared framework.** judgment: the engine already owns every
  relevant resource; another owner adds indirection without a distinct responsibility.
- **Change shutdown policy while moving it.** judgment: a responsibility move does not justify
  altering established admission, cancellation, cleanup retry or first-attach contracts.
