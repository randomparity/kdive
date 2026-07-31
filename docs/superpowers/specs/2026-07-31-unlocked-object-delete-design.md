# Unlocked object-store delete design

## Scope

Issue #1751 covers exactly three real object-store delete sites:

- `jobs/handlers/artifacts/rootfs_reclaim.py`;
- `reconciler/cleanup/uploads.py`;
- `reconciler/cleanup/upload_orphans.py`.

The change may add the shared database fence and adjust publication entry points that must honor
it. It does not change retention thresholds, object naming, upload declaration formats, provider
ports, or MCP authorization.

## Required outcome

No site holds a PostgreSQL transaction or advisory lock while `store.delete` executes. The change
must retain the existing safety property: a delete cannot destroy an object that a concurrent
publisher or rootfs consumer has made live. Rootfs reclaim and both upload reapers need explicit
failure and crash-recovery contracts. Focused tests must prove the lock span, the race ordering,
and delete-failure convergence at all three sites.

## Assumptions

- `ObjectStore.delete` is idempotent for an absent key.
- Supported S3-compatible stores do not provide a usable identity-conditioned delete; ADR-0497's
  MinIO measurements remain governing evidence.
- PostgreSQL exposes `pid` and `backend_start` in `pg_stat_activity` to the application role. Those
  fields identify a backend incarnation without depending on a Python or database wall-clock
  timeout.
- Production upload-window creation, write-lease creation, rootfs reclaim, and both upload reapers
  take the owner advisory lock named by `upload_manifest.lock_scope_for`.

## Approaches considered

### Durable delete claim owned by a database backend — selected

Create a per-key database claim under the owner lock, commit it, and perform the delete while the
claim's dedicated database backend remains alive but idle. Publishers check the claim under the
same owner lock before gaining write authority. A later pass adopts only a claim whose exact
backend incarnation has disappeared.

This preserves total ordering without holding a transaction or advisory lock across the store and
needs no time estimate. It costs one additional short-lived connection during each serial delete.

### Optimistic delete followed by verification

Delete unlocked, then re-read the rows and repair any publisher that raced. This cannot restore
destroyed bytes, and some writes are presigned client PUTs the server cannot replay. It detects a
subset of failures after data loss rather than preventing them.

### Expiring deletion lease

Write a claim with a deadline and let another pass take over after it. A blocking call in
`asyncio.to_thread` is not canceled when its waiter times out. The old delete can therefore land
after takeover, claim release, and a new PUT. No configured duration closes that ordering.

## Data model and shared protocol

Migration `0091` adds `object_delete_claims` with:

- owner kind, owner UUID, and object key as the primary key;
- an opaque claim token used for compare-and-delete finalization;
- claimant backend PID and backend start timestamp;
- creation time for diagnosis only, never for takeover.

The shared `artifacts.object_delete_claim` module owns:

- opening and closing the dedicated connection;
- reading the current backend identity;
- detecting whether an existing claimant still appears in `pg_stat_activity`;
- inserting a new claim or adopting a dead claim under the owner lock;
- token-guarded completion and abandonment;
- querying whether a publication is blocked by a claim.

The API must leave the dedicated connection open from claim commit through delete completion. It
must close the connection on every exit. A failed final database update leaves a stale claim by
construction, so the next pass replays the idempotent delete before reopening publication.

## Rootfs reclaim flow

Phase one runs under `LockScope.INVESTIGATION` on the dedicated connection. It re-reads the due
rootfs row, referencer and fetch-lease gates, and the live staging-partial gate. If any gate pins
the checksum, it commits nothing and declines. Otherwise it unlinks the staged base, creates the
delete claim, and deletes the artifact row in the same transaction.

The object delete then runs with the connection idle. A successful delete clears the claim in a
short locked transaction. A delete exception also clears the claim, reports
`infrastructure_failure`, and leaves a rowless object for `repair_leaked_upload_objects`. If the
clear fails, the connection closes and the claim becomes adoptable. A crash in any post-commit
position is equivalent: the artifact row stays absent, and the next pass adopts the claim and
replays the delete.

Deleting the row in phase one preserves consumer ordering. A fetch that started first holds a
`rootfs_fetch_leases` row and blocks phase one. A later fetch cannot resolve the artifact. The
existing investigation lock still serializes phase one against System binding; once phase one
commits, a later provision sees the same missing checksum it sees after an ordinary completed
reclaim.

The old application timeout is removed because timing out the await does not stop the delete
thread. The object-store client's retry and transport policy bounds the operation without holding
a database lock.

## Upload reaper flow

For each key returned by the row-first manifest claim, open the dedicated connection and attempt
the owner lock. Under that short lock, re-run `owner_key_is_fenced` and create/adopt the deletion
claim. A contended owner, a live existing claim, an artifacts row, a new manifest, or a live write
lease declines the key.

The delete runs unlocked. Success clears the claim and counts `deleted`. A store or database
failure counts `undeleted`; the expired manifest is already gone, so the object remains in the
prefix-driven orphan sweep's candidate set. A dead claimant is adopted and the delete replayed.

## Upload-orphan flow

The sweep keeps its fresh HEAD and mtime/ETag observation. Under a short owner lock it re-runs
`reclaimable_upload_keys` and creates/adopts the claim. It then deletes unlocked. Success clears
the claim and logs the ETag that was classified. Failure clears the claim when possible and counts
the key as failed; the next prefix listing rediscovers it.

## Publication participation

Upload-window creation already holds the owner lock. Before replacing the manifest and returning
presigned PUT grants, it checks that the owner has no delete claim. A claim produces an
`infrastructure_failure` with the same create-upload tool as the recovery action; retrying after
the cleanup pass is the complete recovery contract.

`hold_write_lease` performs the same check inside its existing short owner-locked transaction.
`control.capture_traffic` takes its job-scoped write lease before the deterministic pcap PUT. On a
successful or peer-won registration, release is in the row-protecting transaction; on cancellation
the existing fenced compensating delete runs before release. Failure paths retain the lease for
the stale-lease reaper, matching vmcore capture's crash contract.

## Failure matrix

| site | database decision | delete fails | crash after delete |
|---|---|---|---|
| rootfs | base unlinked, row removed, claim committed | rowless object; orphan sweep retries | stale claim adopted; idempotent replay |
| upload reaper | manifest already removed, claim committed | rowless object; orphan sweep retries | stale claim adopted; idempotent replay |
| upload orphan | listing remains the worklist, claim committed | next listing retries | stale claim adopted; idempotent replay |

No path clears a claim before its delete call has returned. Claim cleanup is token-guarded so a
stale claimant cannot clear a claim that a later pass adopted.

## Testing

Focused PostgreSQL-backed tests cover each site:

- a store callback queries `pg_locks` for the claimant backend at delete time and proves the
  control can observe an advisory lock while the callback observes zero;
- a concurrent publication landing after phase one is rejected or deferred until deletion
  completes, then succeeds without being deleted;
- a delete exception leaves the documented database/object state and a later pass converges;
- a dead-backend claim is adopted, while a live-backend claim prevents a second delete;
- rootfs's row-first phase makes a concurrent fetch resolve no object;
- capture traffic holds a live write lease over its unlocked PUT.

For each new regression, temporarily break the production guard, observe the focused test fail for
the intended assertion, restore the implementation, and rerun it green.

## Sweep evidence

The implementation review searches the Python tree for `store.delete`, `.delete(` on store-like
ports, and `asyncio.to_thread(...delete...)`, then inspects every match nested beneath
`advisory_xact_lock`, `try_advisory_xact_lock`, or `conn.transaction`. The three issue sites are the
only real object-store deletes in that intersection; provider volume/snapshot deletes and database
repository deletes are different verbs.

## Threat model

The change adds no new entry point or actor. Existing authenticated tenants control upload object
names only through validated declarations; keys remain server-derived and SQL remains
parameterized. The trust crossings are PostgreSQL-to-object-store deletion and PostgreSQL
publication grants. The delete claim is the control joining them: a trusted worker or reconciler
may delete only after committed row fences pass, while a publisher receives write authority only
when no claim exists. Failure details name operation and owner/key but expose no credentials.

Out of scope are compromise of PostgreSQL or object-store administrator credentials, and an
undeclared writer that bypasses both upload manifests and write leases. The implementation must
not add such a writer; capture traffic is brought onto the write-lease path in this change.
