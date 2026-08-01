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
versioning with no exclusions when they are created. Operator-provisioned buckets remain
operator-owned; every KDIVE runtime that uses the store validates the standard bucket status before
accepting work and fails with an actionable configuration error rather than changing external
bucket policy.

The standard `GetBucketVersioning` response cannot report MinIO's non-standard prefix exclusions.
An external endpoint is supported only when `Enabled` applies to the whole bucket. The operator must
verify that provider-specific condition; KDIVE cannot claim to detect it through the S3 API. A
configured endpoint that assigns mutable `null` versions under an excluded prefix violates the
contract and can invalidate version-specific deletion.

The object-store port exposes immutable version inventory and deletion:

- exact-key and paged-prefix listings return data versions and delete markers with their VersionId,
  store mtime, and latest/marker flags;
- PUT and HEAD results expose the observed VersionId without persisting it in PostgreSQL;
- `delete_version(key, version_id)` deletes only a caller-selected identity, including the reserved
  `null` value for objects that predate versioning; and
- a bounded exact-key capture returns at most the caller's remaining version budget plus whether
  older exact-key entries remain. Deleting that batch removes nonlatest entries first and removes
  its latest entry only when the capture reached the end of the key history.

Keeping the latest entry until last is failure-atomic for visibility: a partial failure leaves the
same current data or delete marker in place instead of exposing older bytes. A later PUT has a new
VersionId and cannot be targeted by a caller holding an earlier PUT or HEAD result. A write
concurrent with paginated key-history capture may or may not enter a batch, so only a key proven
permanently retired may use that operation. Identity-sensitive compensation always deletes the
VersionId selected by the observation and database fence that licensed it.

The three issue paths split their work around the advisory lock. They capture immutable deletion
targets without a database transaction, re-check their existing database fences and commit their
row decision under the short owner lock, then delete only the captured VersionIds after the lock is
released. An ambiguous response is safe to retry because it can affect only the same immutable
version.

The upload-orphan sweep enumerates `ListObjectVersions`, not only the current logical objects. It
groups work by exact key and spends the existing 200-target per-root budget on captured versions.
When a history exceeds the remaining budget, the sweep deletes only captured nonlatest entries and
leaves the latest entry for a later pass. Peak memory remains one 1,000-entry store page and one
bounded batch, preserving ADR-0498; no operation buffers a complete unbounded history. The sweep
therefore rediscovers failed deletes, noncurrent versions, legacy `null` versions, and delete markers
under the upload roots. The object store is the durable worklist; no PostgreSQL deletion queue is
added. Database artifact/manifest/write-lease fences remain key-scoped, so a live key conservatively
protects all of its versions until the key is no longer reachable.

There is no key-only convenience operation. Existing consumers are assigned explicitly:

- `discard_unregistered_objects`, chunk-upload compensation, and leaked-image repair delete the
  VersionId returned by the PUT or final HEAD whose ETag and row fence they already check;
- the three issue paths, row-driven report/build/investigation garbage collection, private-image
  retirement, System console/SysRq/sidecar teardown, and remote-console part retirement use bounded
  batches only after their existing lifecycle decision proves the logical key retired.

The raw client must never issue key-only `DeleteObject` in KDIVE production code.

The first rollout is an explicit stop-old-first maintenance window, not an ordinary rolling update.
The operator quiesces every KDIVE writer and deleter, installs and verifies the new version
inspection/list/delete permissions, enables bucket versioning, and waits for the provider's
documented activation barrier. For Amazon S3 this includes its documented propagation interval;
managed MinIO initialization completes `mc version enable` and verifies the resulting status before
starting KDIVE. Only the version-aware image then starts. This prevents old key-delete callers from
creating markers over writes during mixed-version service.

Once the contract is active, deployments may roll between versions that implement ADR-0524.
Rolling back to a pre-ADR image is unsupported while the service is live: the old image would turn
permanent cleanup into marker creation and can hide a concurrent PUT. Diagnosis with the old image
requires the same quiesced maintenance boundary; recovery is a forward deployment of a
version-aware image. Suspending versioning is never a rollback action.

The runtime credential needs bucket-version inspection, version listing, and version deletion in
addition to its existing object permissions. Object Lock or a per-version deny remains an ordinary
infrastructure failure: the version remains visible to a later sweep and the pass reports the
failure.

## Consequences

- No object-store delete runs under a PostgreSQL transaction or advisory lock at the three issue
  sites, and delayed deletes cannot target a later PUT.
- Every stored version is a full billable object until version-aware cleanup removes it. Cleanup
  and test-bucket teardown must enumerate versions and markers rather than current keys alone.
- Version-history cleanup retains at most one store page plus the caller's explicit version budget
  and may take several reconciler passes for a hot key. A fault or exhausted budget leaves the
  latest entry untouched.
- A live database reference protects all versions of its key. This may temporarily retain obsolete
  versions, but avoids guessing which version a legacy key-only row intended.
- The standard S3 API validates bucket status but not provider-specific prefix exclusions. External
  operators own that verification; managed KDIVE MinIO never configures exclusions.
- Externally managed deployments gain a pre-deploy prerequisite and IAM permissions:
  `GetBucketVersioning`, `ListBucketVersions`, and `DeleteObjectVersion`.
- The first adoption needs downtime and cannot be rolled back to the prior image while accepting
  work. Subsequent ADR-0524-aware releases retain the normal rolling contract.
- No database migration, write-path VersionId column, compatibility shim, or new dependency is
  introduced.

## Considered & rejected

- **Keep lock-held key deletes.** This remains data-safe but preserves the reported lock-latency
  defect.
- **Database-owned deletion claims.** Backend death and claim takeover cannot prove an earlier
  key-only request is quiescent, so publication can reopen before that request lands.
- **Persist a VersionId on every artifact row.** It would require a schema migration. Ephemeral PUT
  and HEAD results give compensating callers the identity they need, while version inventory gives
  retired-key cleanup its crash-retry worklist.
- **Add a PostgreSQL deletion-obligation queue.** `ListObjectVersions` durably enumerates every
  surviving target, including hidden noncurrent versions and markers. A second inventory would add
  reconciliation states without closing another race.
- **Automatically enable external buckets.** It widens runtime credentials to bucket-policy
  mutation and changes operator-owned state during startup. Provisioning owns mutation; runtime
  assembly owns validation.
- **Treat standard bucket status as proof that MinIO has no exclusions.** `GetBucketVersioning`
  exposes only status and MFA Delete. Provider-specific exclusions are an explicit external
  prerequisite, not something the standard client can fail fast on honestly.
- **Continue key-only delete after enabling versioning.** It creates markers, does not reclaim
  bytes, and makes the contract appear successful while storage grows.
- **Recapture every version after an identity fence.** A peer PUT can enter that later inventory and
  be deleted even though the caller approved only its own VersionId. Selected-version deletion
  preserves the identity fence.
- **Buffer a complete hot-key history.** A deterministic key can have arbitrarily many versions.
  Bounded batches remove nonlatest entries incrementally and never delete latest until a batch
  proves no older entry remains.
