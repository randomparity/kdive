# Concurrent provider diagnostics under one report deadline

## Problem

The composite worker-check dispatcher awaits provider dispatchers serially.
A stalled first provider can consume the shared worker deadline, causing a later
healthy provider to report worker-unavailable without ever enqueuing. The existing
three-provider deadline test explicitly expects enqueue counts `[1, 1, 0]`.

## Scope

Change only `_CompositeWorkerCheckDispatcher.run_worker_checks` and its direct
behavioral tests. Start one asyncio task per selected dispatcher, passing the same
absolute deadline to each. Await them together and flatten results in registration
order. The selected provider contributions are the finite deployment configuration;
production assembly currently contributes local then remote. No new concurrency limit.

On caller cancellation or unexpected child failure, cancel and await the owned
child tasks before re-raising the initiating exception. Preserve that exception's
identity and type, so use explicit owned tasks and gather rather than TaskGroup's
ExceptionGroup behavior. Ordinary queue errors are already diagnostic results and
remain so. Per-provider dispatchers retain budget calculation, enqueue/poll/sleep
classification, and cooperative cancellation. No durable job cancellation is added.

Keep ADR-0854's server/worker phase split, shared deadline, per-provider budget,
expired-no-enqueue policy and result categories. ADR-0865 revises only its serial
execution choice; registration order remains the public result order. No constructor,
MCP field, worker port, migration, auth, dependency or platform-floor change.

## Success

1. Selected dispatchers start without waiting for an earlier dispatcher's completion;
   a healthy third provider reports its real result despite a blocked second provider.
2. The same deadline reaches each selected dispatcher. Existing per-provider budgets
   and server opportunity reservation still bound the worker phase. An already expired
   deadline produces attributed errors with no queue enqueue.
3. Output ordering follows registration order, including when completion order differs;
   an empty selected set returns an empty result list.
4. Caller cancellation and unexpected child failure propagate after owned polling
   tasks settle. Existing queued/running diagnostic jobs are not canceled or changed.

## Failure model

- Actors/deployments: platform-operator diagnostics, server-side composition and
  worker queue on supported Linux x86_64/ppc64le. Local proof uses x86_64 with
  disposable PostgreSQL; production contributions are trusted operator configuration.
- Invariants/assets: healthy provider opportunity, bounded report time, attributed
  errors, stable result order, exception identity and no orphaned local polling tasks.
- Accepted classes: cooperative async timeout/cancellation, not hard real-time
  enforcement; durable diagnostic jobs may outlive a timed-out report per ADR-0854.
- Covered elsewhere: authentication/role checks by ops.diagnostics, queue credential
  fences and worker execution by existing worker/queue tests; live hardware probes
  retain their existing environment requirements and are not claimed by fake endpoints.

## Validation

- `focused-test`: modify existing three-provider actual JobWorkerCheckDispatcher test
  to require `[1, 1, 1]` and the final healthy result; old serial source fails that case.
- `focused-test`: event-controlled dispatchers prove starts before first completion,
  reverse completion with registration-order results, empty set, identical deadlines,
  and expired deadline/no enqueue across all selected actual dispatchers.
- `focused-test`: event-controlled blocked children prove caller cancellation and
  unexpected failure settle siblings and propagate the exact initiating exception.
  Existing queue failure/category and deadline reservation cases remain unchanged.
- Run the full diagnostics test directory and actual MCP diagnostics and worker-handler
  callers with real disposable DB, plus existing diagnostic contribution assembly tests.
- Restore serial dispatch as a controlled fault: progress tests must fail. Restore
  exact source and rerun focused cases. Use `just lint`, whole-tree `just type`, hooks,
  fresh design/code/security reviews and local conventional commit.
- Follow actual next for warranted changed-source Mid elegance assessment; retain
  honest scores and new findings. No unchanged-source rerolls or manual ratings.
