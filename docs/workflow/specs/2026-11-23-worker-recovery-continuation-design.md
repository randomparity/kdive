# Continue committed external-boot recovery checkpoints

## Problem

The worker commits `recovery-attempt` but stops before recovery or conflict resolution reaches
its authority executor. The real Worker/router/handler/PostgreSQL reproduction has two failures;
91 existing focused tests pass because handler re-entry tests manually perform the second call.
[ADR-0845](../../adr/0845-continue-committed-recovery-checkpoints.md) records the bounded decision.

## Scope and behavior

In `jobs/worker.py::_commit_external_result`, accept a success operation of `deadline` or
`recovery-attempt` as a continuation only when the existing authenticated commit returns a Job
whose state is RUNNING. Keep the current handler Job, attempt and credential unchanged.
Change `_run_handler`'s repeated-intermediate warning to name a checkpoint rather than deadline.
Retain its two iterations, all classification/commit paths, heartbeat, cancellation and leases.
No handler, provider, SQL, public metadata, registry, schema or migration changes are needed.

## Failure model

- A superseded or refused commit, missing result, terminal Job or failure never continues.
- Cancellation propagates and cannot start another handler invocation.
- A repeated checkpoint stops after two invocations and leaves the existing lease for recovery.
- An interruption after a durable checkpoint still relies on existing lease/re-entry recovery.
- The authority executor remains an injected external boundary in ordinary tests; no claim of
  live remote-provider or POWER proof is made by the local PostgreSQL regression.

## Success and verification

Promote the two-case private reproduction to `tests/jobs/test_worker_recovery_continuation.py`.
Drive the real router, handler, Worker finalization and PostgreSQL commit using existing authority
fixtures. At executor entry verify the checkpoint and current attempt were persisted first.
After dispatch assert exactly one execute call, succeeded Job, recovered activation and unchanged
job ID, attempt and holder. Cover initial active recovery and parked conflict resolution.

Extend focused worker-model cases to cover both intermediate operations, repeated checkpoint
bounds, superseded and refused commits, terminal jobs, mismatches and cancellation. Keep ordinary,
System-authority, activation readiness and derived-release behavior covered by existing tests.
Use controlled predicate, state and bound faults and restore exact source bytes before gates.

Run relevant worker/model/handler/queue/release tests with Docker required, lint and whole-tree
types, documentation checks, record checks and hooks. Compare worker AST outside the
predicate/warning and preserve all migration digests. Build the actual wheel and exercise its continuation predicate. Fresh scoped
code/security reviews precede queue closure; final campaign CI and review remain required.
