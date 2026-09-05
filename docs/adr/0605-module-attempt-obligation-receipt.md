# 0605 — Verify committed module-attempt intent at the worker

## Status

Proposed (2026-09-05)

## Context

ADR-0588 requires one durable mutation-obligation row to commit before either source or scratch
volume is created. The row is writable only by the server role, while provider volume creation
runs in a worker. No current job kind or payload carries proof across that role boundary, and a
synchronous callback in the libvirt helper cannot await or prove the server transaction.

The eventual orchestration belongs to #2173. This prerequisite must define a carrier that #2173
can embed unchanged and a verifier #2170 can require, without adding either issue's behavior here.

## Decision

The server opens and confirms the exact `(system_id, run_id, operation_nonce)` mutation obligation
inside a transaction it owns. It returns a dispatchable request only after that transaction exits
successfully. Replay against the same open row returns an equivalent request; a discharged row is
not reopened.

The request contains one closed, strict, versioned, canonical JSON receipt naming that exact
attempt tuple. The receipt is evidence to look up, not a bearer capability. It contains no secret,
host, path, credential, or operator identifier.

Before remote-module volume preparation, a worker opens a transaction, acquires the existing
transaction-scoped System advisory lock, compares the receipt with the enclosing operation's
expected attempt tuple, and uses its read-only database authority to verify that the receipt's
exact row exists and its mutation obligation remains open. Only that successful check constructs a
distinct immutable authorization value accepted by #2170's synchronous volume helper. The worker
holds the transaction and System lock while that helper creates both volumes, then releases them.

The existing `discharge_mutation_obligation` repository method acquires the same System advisory
lock before its update. Because every production discharge goes through that repository contract,
callers cannot omit the fence accidentally. The repository still leaves transaction ownership to
its caller, and the lock remains held until that transaction commits or rolls back. Direct SQL by
a process holding server database authority is outside this trusted repository contract.

A production runtime gate unwraps only the verified authorization type for #2170; a request, raw
receipt, boolean, callback, or coroutine is rejected rather than treated as authorization.
Missing, mismatched, malformed, discharged, or unreadable state fails closed before either volume
creation with a redacted error.

The receipt/request decoder is bounded to 4,096 bytes and rejects unknown fields, wrong versions or
types, and noncanonical encodings. Migration 0126's server-write/worker-read privileges remain
unchanged. This decision adds no job kind, handler, migration, libvirt operation, discharge event,
or reaping behavior; it serializes the already-existing discharge repository operation.

## Consequences

The server-to-worker handoff becomes replayable without granting the worker obligation-write
authority. A committed row with no volume remains normal crash residue, while a receipt without a
matching open row cannot authorize storage mutation. Every preparation pays one exact read and
holds the per-System serialization lock while the shared two-volume helper runs.

The Python authorization type prevents accidental bypass within the provider composition; it is
not a cryptographic boundary against arbitrary code executing inside the worker. The database role
and exact-row read are the authority boundary.

#2173 must serialize the version-1 request field unchanged, #2170 must require one verified value
before its first lookup/create sequence, and #2172/#2168 retain discharge/reaping ownership. #2172
uses the now-self-fencing repository method rather than owning a second lock protocol.

## Considered & rejected

- **Let the worker open the row.** verified: migration 0126 grants
  `remote_module_attempt_obligations` write authority to `kdive_server` and only `SELECT` to
  `kdive_worker`; widening that grant would collapse admission and execution authority.
- **Return a receipt while the caller still owns an open transaction.** judgment: the caller could
  dispatch it before commit, recreating the no-owner storage window ADR-0588 closes.
- **Sign the receipt and skip the database read.** judgment: a signature cannot show that the row
  remains open after discharge, while adding key lifecycle to an internal durable-state check.
- **Pass a callback or boolean to the synchronous volume helper.** verified: the existing
  repository open is asynchronous and caller-transactional
  (`src/kdive/db/remote_module_attempt_obligations.py`), so neither value proves the transaction
  committed before the helper runs.
- **Verify, release the database transaction, then create.** judgment: teardown or another terminal
  path could discharge the row between the read and either create, turning the detached value into
  stale authorization. Holding the existing System lock through the helper closes that interval.
- **Lock the exact row instead of taking an advisory lock.** verified: a PostgreSQL 17 probe using
  the real `kdive_worker` login found `FOR KEY SHARE`, `FOR SHARE`, `FOR NO KEY UPDATE`, and
  `FOR UPDATE` all denied by migration 0126's select-only grant. Enabling one would add a migration
  and permission surface that the receipt does not need.
- **Wait for #2173 and define the contract inside its job payload.** judgment: combining contract
  ownership with orchestration recreates the scope collision for which #2251 was split from #2170.
