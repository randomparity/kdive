# 0845 — Continue committed recovery checkpoints

## Status

Accepted (2026-11-23)

> **Superseded by [0853](0853-external-boot-commit-continuation-disposition.md)** (2026-11-23)

## Context

External-boot handlers commit an intermediate success before invoking authority operations.
Activation uses `deadline`; recovery and conflict resolution use `recovery-attempt`.
The worker continues only after `deadline`, leaving both recovery paths running without calling
an authority executor. Existing handler re-entry tests call the handler again themselves.

## Decision

Continue after either existing intermediate operation when its authenticated result commit
returns a running Job. Keep the same claim and the existing maximum of two handler invocations.
The second invocation reads the committed checkpoint through the existing handler path.
Generalize the repeated-checkpoint warning without changing lease recovery or queue policy.
Implemented in `jobs/worker.py` at the existing result-commit and bounded-dispatch seams.
Do not continue on a lost fence, classified refusal, terminal result, failure or cancellation.

## Consequences

Recovery and conflict resolution reach their authority operation without waiting for lease expiry.
No protocol, SQL, migration, public API or new continuation type is introduced. A repeated
checkpoint still leaves the current lease for recovery after the existing bounded dispatch.

## Considered & rejected

- **Wait for lease recovery.** verified: the private real Worker/PostgreSQL proof on
  `cb058d532` produced two failures: the activation became recovering, the job stayed running,
  and both authority executors had zero calls. On Linuxx86_64, the exact command was
  `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest .desloppify/repro_recovery_continuation.py
  -q --tb=short` (one shell command), yielding two failures in5.40s. The permanent regression
  covers the same path; handler-only re-entry does not exercise this decision.
- **Unbounded continuation loop.** judgment: an unexpected repeated checkpoint must not keep
  one dispatch busy indefinitely; the existing two-invocation bound covers this two-stage flow.
- **New continuation result protocol.** judgment: the existing typed success operations and
  committed running Job already carry the necessary decision without another interface.
