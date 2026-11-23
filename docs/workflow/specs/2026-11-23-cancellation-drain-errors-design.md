# Cancellation drain errors

## Problem

Python 3.14 shield installs an error-reporting callback when a caller cancels
its outer future before the inner task fails. Retrieving the task exception
afterward does not remove that callback. Actual power and install helper
regressions report ValueError exception in shielded future despite draining
the provider and preserving their caller-visible cancellation outcome.

## Scope

Retain the existing power and install drain owners and their callers. Follow
the device mutation owner's existing non-raising asyncio.gather pattern;
shield a gather(task, return_exceptions=True) future rather than the task.
Do not add a shared utility: power rethrows the original caller cancellation
and returns T, while install records cancellation and leaves task.result()
to its existing ordered-cleanup caller. These are distinct short policies.

Power creates the gather immediately after its fenced task. Await its shield;
after caller cancellation, repeatedly shield the same non-raising gather
until drained, then rethrow the original cancellation. On the ordinary path,
return task.result() so the original provider result or failure propagates.
Retain provider-kind transfer, System session lock and autocommit restoration.

Install retains its already-done fast path with no added suspension point.
For a pending task create one gather with return_exceptions=True, shield it
through repeated caller cancellation, and return the existing cancellation
boolean. A cancelled child does not count as caller cancellation. The existing
_wait_through_cancellation and _settle_task still read task.result(); provider,
abandonment and build-use release ordering and error precedence stay intact.
No direct shield(task) or newly swallowed ordinary error remains in these two
helpers. This repairs their existing drain contract, requiring no new ADR,
API, schema, permission, dependency, deadline or deployment change.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public tool contracts preserved; local changes and commits only.

## Failure model

- Actors and deployments: worker power and install jobs in local, Compose and
  Kubernetes deployments; ordinary provider code may run in a worker thread;
  worker shutdown or execution cancellation may repeatedly cancel its caller.
- Invariants and assets: the power fence protects a running external mutation;
  install build-use and claim lifetimes outlast provider I/O; original task
  results/errors remain available to the existing owner, without loop errors.
- Accepted failure classes: a provider that never completes retains its fence
  as before; process death and interpreter-fatal SystemExit/KeyboardInterrupt
  cannot be made into cooperative cleanup by an in-process drain helper.
- Covered elsewhere: existing worker failure/retry and durable repair owners;
  existing install cleanup-order tests; tenant and worker authorization gates.

## Success

1. Both helpers drain late ordinary provider errors after caller cancellation
   without emitting an unhandled error to the event loop.
2. Repeated power cancellation retains its lock and connection mode until
   completion, then preserves the original caller cancellation and metric tag.
3. Install keeps normal result/error, child cancellation and already-done
   semantics while preserving ordered provider/abandonment/build-use release.

## Validation

Use an AnyIO test runner that fails on unhandled loop exceptions, with real
threaded operations released by events. Power tests exercise power_handler
with real kdive_worker connections and System lock probes for RESUME and OFF,
with provider success/error. Promote the install provider-error cleanup case
to an async AnyIO test; retain its existing boundary fakes and real thread.
Add focused helper cases for child cancellation, completed tasks and ordinary
result/error. Reproduce the shield errors before implementation. Reinstate
each old shield path as a controlled fault, then restore and run both modules.
Run just lint, whole-tree just type and installed commit hooks.
