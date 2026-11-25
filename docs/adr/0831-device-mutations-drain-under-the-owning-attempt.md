# 0831 — Device mutations drain under the owning attempt

## Status

Accepted (2026-11-23)

Supersedes only ADR-0823's provider timeout and worker
ownership clauses. Its device kinds, public schemas, admission and lifecycle
policies remain in force, including ADR-0828's separate forced-unplug gate.

## Context

At 441b514e5, device handlers bound asyncio.to_thread with wait_for, which
cancels the waiter while its mutation continues. A private event-gated real
thread probe observed terminal timeout before thread completion. The provider
port has no timeout argument. Libvirt disk is_attached also removes storage
on absence; treating it as a harmless observation leaves that mutation loose.

## Decision

Use the existing System session advisory key around a device attempt, sharing
admission's transaction key. Drain the shielded operation through repeated caller
cancellation, then release the lock and restore the connection mode. Check the
persisted running job incarnation after acquiring the lock and in short,
job-row-locked transactions around ledger writes. Stale attempts do not call a
provider after waiting for the lock and do not change another attempt's ledger.

The 60-second plug and 30-second unplug windows bound observation, per attempt
from the start of the provider step, excluding queue and System-lock wait.
An already-started mutation runs to completion under the lock. A definitive
outcome may settle after the window; a pending outcome with no observation
budget fails settle_timeout while retaining available plug evidence. Disk
absence cleanup counts as mutation. NIC presence polling remains read-only
and bounded by the remaining observation window. Public fields and error
vocabulary remain unchanged; agent guidance states this limit precisely.

## Consequences

Timeout and cancellation can delay settlement until a started mutation returns;
there is no invented hard-stop guarantee for synchronous libvirt. Job row locks
cover database writes only, allowing heartbeats during provider I/O. Process or
lock-session loss remains outside this in-process guarantee and belongs to
worker-lifecycle fencing and operator recovery. A forward migration adds a
credential-bound read/row-lock function for device jobs. It reuses
credential-derived authenticate_worker_incarnation, locks only the exact running
device claim, and never changes queue columns or renews its lease. Workers retain
no direct UPDATE on jobs; only their role may execute the new function. The
existing handler assembly supplies its incarnation credential to device handlers.

## Considered & rejected

- **Cancel the thread awaiter at the window.** verified: the event-gated
  asyncio.to_thread/wait_for probe at 441b514e5 fails because the mutation is
  still running when terminal timeout is returned, on Linux x86_64/Python3.14.
- **Add provider deadline/cancellation methods.** judgment: a wider port and
  provider redesign for an API that cannot currently interrupt synchronous
  libvirt calls; awaiting their existing completion is the smaller safe change.
- **Use a plain worker SELECT FOR UPDATE.** verified: an actual kdive_worker
  connection at 441b514e5 raises InsufficientPrivilege for jobs, as migration
  0107 deliberately grants only SELECT. A narrow definer function can lock the
  row while preserving credential-derived ownership and denied direct writes.
- **Use heartbeat as the ledger lock.** verified: heartbeat_worker_job in the
  migrated schema also renews the lease; the new read/lock primitive reuses its
  existing authenticated incarnation lookup without that queue mutation.
- **Hold a database transaction during provider I/O.** judgment: unnecessary
  row-lock lifetime and heartbeat contention; the existing session key and
  short write transactions express the two different lifetimes directly.
