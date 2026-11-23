# Device mutation lifetime

## Problem

Device provider threads outlive wait_for timeout or caller cancellation. A later
attempt can race that physical mutation, and the old handler's ledger writes
are keyed only by device ID. The stale-repair admission race was independently
fixed in 441b514e5; this design finishes the remaining execution-queue finding.

## Scope and ownership

Keep the existing device handlers and provider ports. Add a private device-attempt
owner inside jobs/handlers/devices.py, holding the connection and claimed Job.
It validates the persisted running claim after System-lock acquisition, and
checks it under a job row lock in a short transaction before each of these
ledger writes: start window, record plug evidence, settle outcome and delete
row. Match ID, running state, attempt, worker, created_at, kind and JSON payload;
a missing or changed claim becomes a logged no-op. Keep job row locks out of
provider I/O, allowing heartbeats and the existing queue finalizers to proceed.
Workers deliberately lack direct jobs UPDATE, including SELECT FOR UPDATE.
Forward migration 0203 adds lock_device_job_attempt, a worker-only SECURITY
DEFINER read/lock primitive with empty search_path. Reuse the existing
credential-bound authenticate_worker_incarnation function, which checks the
active protocol and holds incarnation locks, then lock only a matching running
plug/graceful-unplug/forced-unplug snapshot. No lease renewal or queue write.
Pass the existing assembly incarnation credential into the internal handlers;
never persist or log it. Keep direct jobs UPDATE denied. Existing provider
signatures and public tool schemas remain unchanged.

The handler owns the existing scoped System session advisory lock, sharing the
admission transaction key. Follow the existing power handler's shield-and-drain
pattern, including repeated cancellation, provider-kind ContextVar handoff,
transaction-free connection precondition and restoration of autocommit mode.
The lock and connection remain owned until the protected operation completes;
caller cancellation stays primary after drain. System/row reads and provider
resolution happen under that lock, after the initial claim validation.

Await plug and graceful/forced unplug mutations without cancelling their thread
at expiry. Check the remaining observation budget before starting them. Record
a successful plug's handle/identity for the current owner before polling, even
when its callback took longer than the window. A definitive settled or removed
outcome may finish successfully after expiry; a pending outcome cannot start a
new observation without budget and fails with the existing settle_timeout.
Keep that row and evidence for retry. For disk is_attached, drain the callback
because absence can free storage; for NIC is_attached retain wait_for's bounded
read-only path. Refusal restoration, redaction and other provider errors retain
their existing behavior. See [ADR-0831](../../adr/0831-device-mutations-drain-under-the-owning-attempt.md).

Move no production responsibility to a new service or generic supervisor. Test
fixtures that call real handlers must create and dequeue real jobs instead of
synthetic or queued-only Job objects. Add a separate concurrency test module
using existing database seed and worker-fence helpers; do not add test bypasses.

## Limits and public contracts

Keep tool/provider signatures, response fields and error vocabulary. Correct the
unimplemented hard deadline promise in the registrar wrapper text, shared agent
resource and guide, and regenerate the reference with just docs. The 60/30-second
limit is per attempt's observation, measured on the worker monotonic clock from
provider-step start, excluding queue/System-lock wait; list_devices reports the
corresponding absolute deadline and server_time. Expiry ends further observation;
an already-started mutation delays settlement until completion. For a pending
outcome, control_failure/settle_timeout and a failed row direct the caller to
list_devices then safe unplug/retry. A known completed outcome remains truthful.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public wire schemas and provider signatures preserved; local commits only.

## Failure model

- Actors and deployments: authenticated tenants enqueue device work; server,
  worker and reconciler share Postgres in local, Compose and Kubernetes modes.
  Providers execute synchronous calls in worker threads; local and remote
  libvirt share the disk cleanup callback, fault-inject exercises both kinds.
- Invariants and assets: System mutation serialization, device backing storage,
  current attempt ledger evidence, callback completion and connection ownership;
  claim checks and ledger mutation commit atomically, without a long transaction.
- Accepted failure classes: a provider that never returns retains its System
  lock until process/connection termination; no fictitious bounded completion.
  Process death or lock-session loss while a thread runs is outside this
  in-process guarantee. Out-of-band claim replacement cannot stop a started
  callback: retain the System lock, then discard stale ledger writes. Read-only
  NIC observations may outlive their cancelled
  waiter because they cannot mutate storage or device state.
- Covered elsewhere: worker-lifecycle authority owns process-loss fencing;
  admission owns authz/capability/idempotency; device-repair locking is covered by
  441b514e5; generic worker finalizers fence job results; authority-specific
  operations and future passthrough kinds retain their own owners.

## Threat model

- Boundaries: tenant requests already cross MCP validation/authorization before
  enqueue; workers read claims and provider output. The new worker-only definer
  function adds a narrow privileged row-lock entrypoint, not a queue-write grant.
- Actors: tenants control request values but not database rows; the operator
  controls workers/providers. Trust the database and registered worker process,
  not a stale in-memory claim or a provider's completion timing.
- Controls: retain load_payload and existing admission gates; bind the exact
  persisted claim in parameterized SQL through the new definer function. Derive
  worker identity from its credential via existing authentication, restrict to
  the three device job kinds, revoke PUBLIC/other-runtime EXECUTE and retain
  direct jobs UPDATE denial. Keep detail redaction and size bound.
  Provider errors retain existing taxonomy and expose no new raw output.
- Out of scope: compromised workers/database operators and loss of the lock
  session; existing deployment authority owns those threats. This is a current-
  attempt concurrency fence, not a new credential or tenant-authorization gate.

## Success and validation

1. Mode: focused-test. Event-gated real plug, graceful/forced unplug and disk
   cleanup callbacks hold the real System key across elapsed window and repeated
   cancellation. A second transaction cannot acquire that key, and the handler
   cannot return until release. Expected red: current handler returns or releases
   ownership while a callback remains active. Preserve definitive and pending
   outcomes and current-owner evidence after release.
2. Mode: focused-test. Replace a persisted claim while the callback is gated;
   after release, handle/state/delete writes leave the new owner's row unchanged.
   Replace a claim while waiting for System lock; no provider call may start.
   Expected red: old handler writes or calls the provider for a stale claim.
3. Mode: focused-test. Existing device handler, ledger, MCP normal/forced admission,
   reconciler and shared libvirt suites prove refusals, redaction, handle-before-
   poll durability, pending detach and read-only timeout behavior on real roles.
4. Mode: focused-test. Cancellation and success preserve caller connection mode
   and provider-kind metric context. Transactional callers fail before I/O.
5. Mode: focused-test. Real kdive_worker executes the new locking primitive;
   wrong credentials, another holder, mismatched snapshot and terminal jobs
   cannot lock an owning claim. Other process roles cannot execute it. Direct
   worker jobs UPDATE/locking SELECT remain denied; matching lock blocks a queue
   writer until commit, without changing the job. Actual role tests first fail
   UndefinedFunction before migration and then pass.
6. Mode: focused-test. Controlled faults remove drain, disk cleanup classification,
   initial claim validation and per-write fences; require named regression reds.
   Restore, run focused tests, lint, whole-tree type, docs/schema checks and hooks.
   A focused native libvirt proof should exercise changed paths when available;
   report exact arms and revisions without claiming remote or real POWER proof.
