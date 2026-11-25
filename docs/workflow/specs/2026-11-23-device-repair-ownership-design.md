# Device repair ownership

## Problem

repair_stalled_device_ops selects transient rows without active owning jobs,
then checks only the device state before failing them. An old unplug attempt
can end and a retry return the row to unplugging with a new job in that gap.
The stale candidate then fails the retry (ADR-0823, ADR-0828).

## Scope

Select each candidate's ID and System ID. In its settlement transaction, take
LockScope.SYSTEM, matching device admission. Recheck the original state and
absence of queued/running jobs of the owning kinds, constrained to that device
and System, before calling the existing guarded settle_device. Hold the lock
through commit. Increment and log only an applied settlement.

Give candidate selection and recheck one shared SQL predicate so their state,
kind and active-job criteria cannot drift. Keep plug ownership separate from
both graceful and forced unplug ownership. Do not add a provider call, schema,
claim token, public interface or generic repair framework. This restores the
existing admission serialization decision; provider-thread lifetime is the
second independent fix in the queue cluster.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public contracts preserved; local commits only.

## Failure model

- Actors and deployments: concurrent server device admission and reconciler
  repair over shared Postgres in local, Compose and Kubernetes deployments.
- Invariants and assets: current device attempt state and owning queued/running
  job; lock precedes fresh predicate read and guarded settlement in one commit.
- Accepted failure classes: a retry admitted after repair commits observes the
  failed old attempt and proceeds normally; selection alone grants no ownership.
- Covered elsewhere: admission authz/capability/idempotency and enqueue atomicity
  remain with the existing MCP layer; provider mutation/cancellation is the next
  independent fix; DB errors propagate and roll back through existing framing.

## Success

1. Graceful and forced unplug retries queued after selection retain their
   unplugging state, detail, deadline and job; repair reports zero.
2. Existing owning-kind, active-state and stranded-device outcomes remain valid.

## Validation

Mode: focused-test. Hold the System lock on a real Postgres connection, start
repair as kdive_reconciler, and use bounded pg_blocking_pids observation to
prove it waits. While holding the lock, end the old attempt and enqueue the
retry in the same transaction that restores unplugging. Release, require zero
repairs and unchanged retry evidence. Expected red: old repair does not wait.
A private source-boundary probe already reproduces actual stale settlement.
Run the full device repair and device MCP suites with Docker required. Remove
System locking and the locked absence check separately; each must fail the new
race. Restore and rerun. Run lint, whole-tree type, docs and installed hooks.
