# 0524 — Durable claims fence object deletes outside database locks

## Status

Proposed (2026-07-31)

Adversarial review blocked acceptance. PostgreSQL-backend disappearance and an ambiguous store
error do not prove that an earlier `DeleteObject` can no longer land. Adopting or clearing the
claim can therefore reopen publication while the earlier request is still in flight, allowing it
to destroy the newly published bytes. The decision below is retained as the reviewed candidate,
not as an implementable contract.

The sound alternative identified by review is deletion by immutable S3 version ID, with a durable
retry obligation for ambiguous failures. It requires bucket versioning as a deployment prerequisite
and provisioning/rollout changes outside #1751's authorized scope. An operator must choose whether
to authorize that storage-contract change or retain the existing lock-held deletes.

## Context

Three reclaim paths call the object store while holding a transaction-scoped advisory lock:

- investigation rootfs reclaim holds `INVESTIGATION` across one delete;
- the expired-upload reaper holds the upload owner's lock across each delete;
- the upload-orphan sweep holds the same owner lock across each delete.

The locks close real publication races. A delete that merely moves after the transaction can race a
new upload window, a job-held write lease, or an artifact-row commit and destroy bytes that became
live after the database re-check. Rootfs reclaim also serializes against a new System binding and
an in-flight rootfs fetch. S3-compatible `DeleteObject` offers no usable identity precondition in
the supported MinIO deployments (ADR-0497), so an ETag check cannot close the interval.

The current closure makes object-store latency database-lock latency. A degraded store therefore
serializes unrelated work in the same owner scope. Rootfs adds an application timeout, but
`asyncio.to_thread` cannot cancel the blocking delete; releasing a database fence at that timeout
would let the abandoned thread delete a later publication.

## Decision

Add `object_delete_claims`, keyed by `(owner_kind, owner_id, object_key)`. A claim records a random
token plus the PostgreSQL backend PID and backend start time of a dedicated connection. The backend
identity makes a claim recoverable without a guessed wall-clock lease: a matching row in
`pg_stat_activity` is live, while a claim whose exact backend incarnation is absent may be adopted
by a later pass.

Each delete uses three phases:

1. Open a dedicated database connection. In a short transaction under the owner's advisory lock,
   re-check the path-specific fences and insert or adopt the delete claim. Rootfs reclaim also
   unlinks the staged base and removes its artifact row in this phase.
2. Commit and call `store.delete` while the dedicated backend is idle. No transaction or advisory
   lock spans the call.
3. In a second short locked transaction, remove the claim. Rootfs completion removes no further
   durable state because its artifact row was retired in phase one.

An existing claim owned by a live backend is a decline, not a second delete. An existing claim
whose recorded backend incarnation is gone is adopted under the owner lock and the idempotent
delete is replayed. A process death after the store delete but before phase three therefore
reconverges without opening a publication window.

The publisher side participates in the same ordering. Upload-window minting and job write-lease
minting already run under the owner lock; both reject an active delete claim before granting write
authority. `control.capture_traffic`, which writes under the upload-orphan sweep's Run prefix but
previously had no durable declaration between its first guard and its PUT, takes its job-scoped
write lease before that PUT and releases it atomically with row registration or after fenced abort
cleanup. Thus either publication authority commits first and the deleter declines, or the delete
claim commits first and publication retries after the claim is gone.

Rootfs reclaim retires the artifact row in phase one. A fetch that started first has already minted
its fetch lease and makes reclaim decline; a fetch that starts later cannot resolve the row. A
System bind that commits later may still name the checksum, as it could after a completed reclaim,
but provisioning fails closed at the same missing-row resolution boundary instead of downloading
an object being deleted.

Delete failures abandon only the current token under the owner lock:

- rootfs keeps the staged base and artifact row removal already committed; the remaining object is
  rowless and is retried by the upload-orphan sweep;
- the upload reaper has already removed the expired manifest, so its failed key remains rowless for
  the upload-orphan sweep;
- the upload-orphan sweep rediscovers its failed key from the next object-store listing.

If abandoning the token cannot commit, closing the dedicated backend turns it into a stale claim;
the next pass adopts it before retrying the delete. The rootfs application timeout is removed:
returning while its uncancellable thread can still delete would invalidate the claim. The store
client's own retry/transport budget now bounds that job, but no database lock is held during it.

## Consequences

- Store latency no longer extends a PostgreSQL transaction or advisory-lock span at any of the
  three sites.
- Migration `0091` adds the claim table and its backend-identity columns. No public schema or new
  dependency is introduced.
- Each delete uses one short-lived PostgreSQL connection in addition to the caller's pooled
  connection. Deletes remain serial within each existing loop, so this is one extra connection at
  a time, not one per backlog item.
- A hung delete keeps its key claimed and makes a new publication fail fast instead of making every
  operation on the owner wait on an advisory lock. Recovery waits for the dedicated backend to
  disappear; it does not guess that a live delete is dead.
- `pg_stat_activity.backend_start` is part of the identity so PostgreSQL PID reuse cannot make a
  dead claimant appear live.
- Rootfs delete failure no longer retains the artifact row. Retaining it would expose a live row
  before an unlocked delete; row-first retirement instead hands the residue to the prefix-driven
  orphan collector.

## Considered & rejected

- **Release the lock and keep only the existing re-checks.** A publisher can commit after the
  re-check and write before the delete. This is the data-loss race ADR-0502 and ADR-0509 closed.
- **Condition the delete on ETag.** ADR-0497 measured `If-Match` as inert on the supported MinIO
  releases. A guard that passes a stub and deletes unconditionally in production is worse than no
  guard.
- **Use an expiring claim.** No safe duration bounds a blocking delete thread. Taking over an
  expired claim while the old delete still runs lets that old call destroy bytes published after
  the takeover completes.
- **Keep the rootfs row until the unlocked delete succeeds.** A concurrent fetch can resolve that
  row and begin reading before the delete. Retiring it in phase one is the fail-closed ordering.
- **Hold a session advisory lock.** It would recover automatically with a backend, but it is still
  a PostgreSQL advisory lock held across object-store I/O and preserves the defect this decision
  removes.
- **Delete an immutable S3 version ID.** This prevents a delayed request from targeting a later PUT
  at the same logical key and is the only unlocked-delete alternative the review found sound. It is
  not selected here because it requires enabling and provisioning bucket versioning, defining a
  mixed-version rollout, and persisting failed version-specific deletion obligations. That is a
  deployment and object-store contract change requiring operator authorization.
- **Keep the status quo.** It remains fail-closed against publication races but leaves store
  latency inside the transaction and owner lock. It is the safe fallback while the versioning
  decision is unauthorized; the issue cannot meet its requested unlocked-delete outcome on that
  fallback.
