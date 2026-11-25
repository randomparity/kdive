# Abandoned job terminalization

## Problem

The reconciler selects expired exhausted jobs, then updates by ID and running
state alone. A heartbeat or recycled claim between those statements can be
failed incorrectly. Its separate Run SQL omits failing_job_id and the worker's
INSTALL policy for a build-succeeded Run (ADR-0500 and ADR-0230).

## Scope

Retain repair_abandoned_jobs as the ordinary-job repair owner. Capture ID,
attempt, worker, creation time, kind and payload during selection. In each
candidate transaction, derive its Run ID and take the existing Run advisory
lock before updating the job, matching the worker's Run-before-job lock order.
Use a conditional UPDATE matching that snapshot and rechecking expired lease,
exhaustion and both authority-marker exclusions at database wall-clock time.
Only RETURNING rows may increment the count, log failure or compensate a Run.
Malformed payloads retain job-only terminalization and log only on success.

Reuse RUNS.record_terminal_failure in the same transaction, including job ID.
Move the worker's existing eligible-state policy into domain/operations/jobs.py
as run_failure_eligible_states(kind). Both writers use CREATED and RUNNING;
INSTALL also accepts SUCCEEDED. Remove their duplicated private state sets.
Keep the worker's failed-job guard. Existing receipt-specific external-boot
and authority-System repairs retain ownership and behavior. No public contract,
permission grant, migration, dependency or deployment changes are required.

A row lock before the Run lock would invert the worker/enqueue ordering.
Rechecking only the lease misses a replacement whose lease has also expired;
the snapshot fence distinguishes it without inventing another claim token.
This restores existing decisions rather than introducing a new architecture.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public tool contracts preserved; local changes and commits only.

## Failure model

- Actors and deployments: server enqueue/recycle, worker heartbeat/finalization,
  and periodic reconciler using Postgres in local, Compose and Kubernetes
  deployments; authenticated tenant requests reach the existing enqueue layer.
- Invariants and assets: protect current job ownership and Run failure metadata;
  preserve Run-before-job locking and atomic candidate transaction rollback.
  Unexpected SQL or permission errors propagate, allowing a later repair retry.
- Accepted failure classes: heartbeat after committed failure cannot revive work,
  as before; selection does not promise to terminate a changed incarnation.
- Covered elsewhere: receipt-specific SQL owns authority job terminalization;
  worker atomicity tests cover its existing rollback and lock ordering;
  existing tenant authorization and worker incarnation gates remain unchanged.

## Success

1. Renewal and recycled-claim races preserve the current job and Run metadata.
2. Applied ordinary failures atomically record the job and eligible Run's
   lease_expired category and failing_job_id, with worker-identical state policy.
3. Malformed payload and authority exclusions preserve their existing behavior.

## Validation

Real Postgres tests hold the Run advisory lock, observe repair blocking through
pg_blocking_pids, commit a heartbeat or recycle/claim on another connection,
then release the lock. Backdate the replacement lease to expose snapshot ABA.
Assert no repair and unchanged job/Run. Parameterize Run compensation across
RUNNING+INSTALL, SUCCEEDED+INSTALL and SUCCEEDED+BOOT using kdive_reconciler.
Keep malformed-payload and authority suites. Existing worker atomicity tests
cover rollback and Run-before-job ordering. Observe red before implementation;
remove the lease check, snapshot fence and INSTALL policy as controlled faults.
Run focused tests, just lint, whole-tree just type and installed commit hooks.
