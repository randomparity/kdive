# 0524 — Require versioned object-store deletion

## Status

Proposed (2026-07-31)

## Context

Three reclaim paths call `DeleteObject` while holding a transaction-scoped PostgreSQL advisory
lock. The lock closes publication races, but makes object-store latency part of the database lock
span. Moving a key-only delete after the transaction is unsafe: an ambiguous or delayed request can
arrive after a later PUT to the same key and delete the newly published bytes.

S3 versioning gives every write an immutable VersionId. A delete naming that VersionId cannot
target a later write. Versioning also changes two details that the contract must handle explicitly:
a key-only delete creates a delete marker instead of reclaiming bytes, and deleting the current
version can expose an older version as current. Objects written before versioning was enabled have
the reserved `null` VersionId.

KDIVE has more key-delete consumers than the three lock-held sites. Making versioning a deployment
prerequisite without changing the shared delete contract would silently turn image, report,
snapshot, build, garbage-collection, and remote-console cleanup into marker creation.

## Decision

The configured artifact bucket must have versioning fully `Enabled`. `Suspended`, an absent status,
and prefix exclusions are unsupported. KDIVE-provisioned development and demo buckets enable
versioning when they are created. Operator-provisioned buckets remain operator-owned; every KDIVE
runtime that uses the store validates the versioning state before accepting work and fails with an
actionable configuration error rather than mutating external bucket policy.

The object-store port exposes immutable version inventory and deletion:

- exact-key and paged-prefix listings return data versions and delete markers with their VersionId,
  store mtime, and latest/marker flags;
- `delete_version(key, version_id)` sends `DeleteObject` with `VersionId`, including the reserved
  `null` value for objects that predate versioning;
- the key-oriented convenience delete first snapshots every exact version and delete marker, then
  deletes only that captured set. It never sends key-only `DeleteObject`; a version written after
  the snapshot survives.

The three issue paths split their work around the advisory lock. They capture immutable deletion
targets without a database transaction, re-check their existing database fences and commit their
row decision under the short owner lock, then delete only the captured VersionIds after the lock is
released. An ambiguous response is safe to retry because it can affect only the same immutable
version.

The upload-orphan sweep enumerates `ListObjectVersions`, not only the current logical objects. It
therefore rediscovers failed deletes, noncurrent versions, legacy `null` versions, and delete markers
under the upload roots. The object store is the durable worklist; no PostgreSQL deletion queue is
added. Database artifact/manifest/write-lease fences remain key-scoped, so a live key conservatively
protects all of its versions until the key is no longer reachable.

All other production object deletes use the shared version-aware convenience operation. The raw
client must never issue key-only `DeleteObject` in KDIVE production code.

Rollout enables bucket versioning before the new application image is started. During a rolling
upgrade, an old replica still performs its lock-held key delete; on the now-versioned bucket that
creates a marker rather than permanently deleting bytes. New replicas enumerate and remove those
markers. Rolling back to the old image remains data-safe but pauses permanent reclamation until the
version-aware image returns.

The runtime credential needs bucket-version inspection, version listing, and version deletion in
addition to its existing object permissions. Object Lock or a per-version deny remains an ordinary
infrastructure failure: the version remains visible to a later sweep and the pass reports the
failure.

## Consequences

- No object-store delete runs under a PostgreSQL transaction or advisory lock at the three issue
  sites, and delayed deletes cannot target a later PUT.
- Every stored version is a full billable object until version-aware cleanup removes it. Cleanup
  and test-bucket teardown must enumerate versions and markers rather than current keys alone.
- A live database reference protects all versions of its key. This may temporarily retain obsolete
  versions, but avoids guessing which version a legacy key-only row intended.
- Externally managed deployments gain a pre-deploy prerequisite and IAM permissions:
  `GetBucketVersioning`, `ListBucketVersions`, and `DeleteObjectVersion`.
- No database migration, write-path VersionId column, compatibility shim, or new dependency is
  introduced.

## Considered & rejected

- **Keep lock-held key deletes.** This remains data-safe but preserves the reported lock-latency
  defect.
- **Database-owned deletion claims.** Backend death and claim takeover cannot prove an earlier
  key-only request is quiescent, so publication can reopen before that request lands.
- **Persist a VersionId on every artifact row.** It would require schema and every write/finalize
  path to change, including presigned PUTs whose VersionId is known only after upload. Version
  inventory already supplies the immutable deletion identity and crash-retry worklist.
- **Add a PostgreSQL deletion-obligation queue.** `ListObjectVersions` durably enumerates every
  surviving target, including hidden noncurrent versions and markers. A second inventory would add
  reconciliation states without closing another race.
- **Automatically enable external buckets.** It widens runtime credentials to bucket-policy
  mutation and changes operator-owned state during startup. Provisioning owns mutation; runtime
  assembly owns validation.
- **Continue key-only delete after enabling versioning.** It creates markers, does not reclaim
  bytes, and makes the contract appear successful while storage grows.
