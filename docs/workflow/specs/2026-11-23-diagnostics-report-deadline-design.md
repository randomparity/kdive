# Diagnostics report deadline

## Problem

The report's overall deadline ends at server checks. Worker enqueue is outside its own local
budget and queue awaits are unbounded. Two source-level reproductions time out at the outer
safety harness instead of returning the expected attributed report. Serial providers renew time.

## Scope

Implement [ADR-0854](../../adr/0854-diagnostics-report-deadline-propagation.md), retaining
ADR-0164's reserved worker phase. The service owns the report deadline and phase partition;
existing dispatchers own their result attribution and queue handling. Extend the internal port
with optional keyword `deadline: float | None = None`, using event-loop monotonic seconds.
Migrate both production implementations and the two inventoried test doubles atomically.
No obsolete compatibility wrapper, new framework, new task fan-out or persistent state is needed.
Public Check/CheckResult values and DiagnosticsService constructor signatures remain unchanged.
Custom injected dispatchers must accept the new optional keyword; this internal port migrates
under [ADR-0041 decision 1](../../adr/0041-versioning-release-process.md), which protects the
MCP surface, ErrorCategory and durable schema/state machines rather than this Python seam.

## Failure model

- Actors and deployments: authenticated operators/CLI and CI call the existing diagnostics tool;
  the server dispatches read-only jobs through Postgres to configured provider workers.
- Invariants and assets: one coherent three-state report; server work cannot consume the reserved
  worker phase; providers cannot renew its deadline; queue awaits cannot wedge the report;
  caller cancellation and completed sibling verdicts survive the unchanged serial execution model.
- Accepted failure classes: cancellation-resistant or synchronous blocking code is outside the
  cooperative async timeout guarantee. Timed-out durable jobs may finish later: these existing
  read-only jobs retain queue/worker lifetime semantics. Runtime scheduling overhead is not a
  hard real-time guarantee. Existing queue failures remain infrastructure errors.
- Covered elsewhere: codec malformed-item isolation is completed Task105; probe implementations
  and worker execution remain their existing owners; renewable external-boot repair budgets
  remain the separate campaign owner. Public auth/schema gates remain unchanged.

## Success

1. Create the report deadline once before server checks. With finite overall time and dispatch,
   reserve min(15 seconds, max(0, overall/2)) for the entire worker phase. Server checks receive
   the remainder. Worker deadline is min(original report deadline, worker-entry time + reserve).
   Without dispatch, server checks retain their whole budget; None keeps existing local bounds.
2. Composite providers receive one identical worker deadline in configured order. Completed
   results are retained. An expired provider dispatch returns one existing attributed error per
   configured worker-check ID, without new enqueue, so unstarted providers remain represented.
3. Job dispatch bounds enqueue, get and sleep by min(propagated deadline, loop time + local budget).
   Use one asyncio timeout context around the existing operation, with a small private operation
   method if needed. Only that context's own expiry maps to worker unavailable; other TimeoutError
   retains current behavior. Caller CancelledError propagates. Start the existing injected elapsed
   clock before enqueue, retaining deterministic polling tests without comparing clock epochs.
4. Queue payload, synthetic principal/provider identity, unique dedup, max_attempts=1, terminal
   decoding, enum/error mapping and valid wire fields remain unchanged. Zero-argument dispatcher
   calls remain supported. No durable job cancellation or completion claim follows report expiry.
5. Wrapper guidance states the default 30-second monotonic per-call orchestration budget, unfinished
   check errors and retry via ops.diagnostics after readiness is restored. Regenerate its reference.

## Validation

Add focused deadline regressions using real service/composite/job-dispatcher classes with injected
queue boundaries. Hung enqueue/get/sleep must return errors before an independent generous safety
harness expires; no tight elapsed-time assertions. Cover expired-before-enqueue, configured local
limit, shared provider deadline, completed sibling retention, server reservation and zero/None limits.
Test caller cancellation with a boundary-entry event, and queue-origin TimeoutError separately.
Keep existing fake elapsed-clock polling tests and actual Postgres enqueue/claim/complete integration.
Controlled missing-timeout/renewed-deadline/lost-reservation faults must fail meaningful contracts.
Run diagnostics, affected MCP tool/schema cases, lint/whole types/hooks/docs/record guards and an
installed-wheel deadline test with verified module origins. Fresh code/security review precedes closure.

## Agent-facing contract evaluation

AI-SPEC: an authenticated operator agent calls ops.diagnostics with existing provider/egress inputs;
output is the existing report, based only on configured checks and worker job results. Describe
bounded orchestration without claiming a timed-out job stopped. No LLM calls or cost changes arise.
Latency is the default 30-second cooperative budget; success is an attributed verdict, not a hang.

| Case | Setup/input | Observable gate |
|---|---|---|
| happy | existing real Postgres diagnostics completion | real worker PASS results; block |
| ambiguous | configured checks cannot reach worker | ERROR plus existing readiness guidance; block |
| unsafe claim | hung queued/running job | no cancellation/completion assertion; ERROR, block |
| stale data | existing malformed/partial worker result | retain valid sibling and attributed errors; block |
| permission | existing unauthorized ops.diagnostics tests | access denied before service call; block |
| cost/loop | expired deadline and multiple providers | no extra enqueue, one phase cap; block |
| regression | actual service plus hung enqueue/get | report returns within safety harness; block |

These are deterministic service/tool checks, not an LLM judge or a new evaluation subsystem.
Generated reference parity verifies the wrapper is the served contract source.
