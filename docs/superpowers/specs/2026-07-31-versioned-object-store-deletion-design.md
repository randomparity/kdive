# Versioned object-store deletion design

## Status and decision

Approved by the operator on 2026-07-31. [ADR-0524](../../adr/0524-versioned-object-store-deletion.md)
records the governing decision: bucket versioning is required, externally managed buckets fail
fast when it is not enabled, and KDIVE deletes immutable VersionIds rather than logical keys.

## Scope

Issue #1751 requires three real object-store deletes to move outside their PostgreSQL transaction
and advisory-lock spans:

- investigation rootfs reclaim;
- expired-upload reaping;
- upload-prefix orphan repair.

Making versioning an object-store contract also owns the direct dependencies needed to keep every
existing delete effective: the shared store/value types, all production key-delete consumers,
runtime validation, Compose and Helm demo provisioning, test bucket lifecycle, IAM/operator docs,
and focused contract/concurrency tests. Retention durations, object-key formats, authorization,
provider selection, and MCP schemas do not change. No database migration or dependency is added.

## Required behavior

### Bucket contract

The configured bucket must report `Status=Enabled` from `GetBucketVersioning`. Missing status,
`Suspended`, and prefix exclusions fail with `configuration_error`; the message names the bucket,
observed state, required state, and recovery action. The check runs before server, worker, or
reconciler work begins and remains part of object-store readiness.

Compose `minio-init`, the Helm bundled-backend initializer, and the live MinIO test fixture enable
versioning immediately after bucket creation. External deployments enable it outside KDIVE before
rolling out the image. KDIVE does not call `PutBucketVersioning` for an external bucket.

The documented runtime policy adds `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion`. Existing read, write, current-key list, and HEAD permissions remain.

### Store values and operations

Add an immutable `ObjectVersion` value carrying:

- `key: str`;
- `version_id: str` (including the literal `null` legacy identifier);
- `last_modified: datetime`;
- `etag: str | None` (`None` for a delete marker);
- `is_latest: bool`;
- `is_delete_marker: bool`.

`iter_prefix_version_pages(prefix)` lazily paginates `ListObjectVersions` with the existing
1,000-entry page bound. It validates fields through the store boundary, combines `Versions` and
`DeleteMarkers`, and preserves the service's per-page order. `list_exact_versions(key)` consumes
that iterator with `Prefix=key`, retains only entries whose key equals `key`, and returns every data
version and marker for the exact key.

`delete_version(key, version_id)` always supplies `VersionId` to `DeleteObject`. Deleting an absent
version is idempotent. A malformed reply or client/transport fault maps to
`infrastructure_failure` and names the key and VersionId without exposing credentials.

The existing key-oriented `delete(key)` remains as a convenience for callers that do not need a
database split. It snapshots `list_exact_versions(key)` completely before issuing any delete, then
deletes every captured VersionId. It never sends key-only `DeleteObject`. A PUT that lands after the
snapshot creates a VersionId outside the captured set and survives.

Current-object APIs (`head`, `get`, `list_prefix`) retain their logical-key behavior. This avoids
leaking versioning concerns into readers and callers that do not delete.

### Rootfs reclaim

Before opening its reclaim transaction, rootfs reclaim snapshots the exact versions of its object
key. Under `LockScope.INVESTIGATION`, it re-reads the due row, referencer/fetch-lease/staging-partial
fences, unlinks the staged base, and removes the artifact row. It commits before deleting any
captured version.

It then calls `delete_version` for the captured targets without an open transaction or advisory
lock. A failed or ambiguous delete returns the existing infrastructure failure; the artifact row
stays retired and every surviving version is discoverable under `local/investigations/` by the
version-aware orphan sweep. There is no application timeout around an uncancellable blocking
thread.

A later PUT to the same key is a different VersionId and cannot be hit by the captured deletes. A
fetch that began first still has a durable lease and blocks row retirement; one that begins later
cannot resolve the removed row.

### Expired-upload reaper

The reaper claims and removes the expired manifest under the existing owner lock without listing or
deleting from the store in that transaction. After commit it snapshots the prefix's version pages.
For each version, a short owner-locked transaction re-runs `owner_key_is_fenced`; it then releases
the transaction and deletes that exact VersionId. Contention or a new artifact, manifest, or live
write lease declines the version for a later pass.

Store/list/delete failures count as undeleted. Since the manifest is already gone, surviving
versions remain candidates for the version-aware orphan sweep.

### Upload-orphan sweep

The sweep walks `local/runs/` and `local/investigations/` through
`iter_prefix_version_pages`. Its 200-unit per-root budget counts versions, not logical keys. It
attributes each version by key, uses the immutable version mtime for the grace test, and removes the
current-key HEAD re-read: a version's mtime and identity cannot change.

Immediately before deletion it attempts the owner lock and re-runs the existing key-scoped artifact,
manifest, and write-lease fences. The transaction ends before `delete_version` is called. A key
with any live database reference conservatively protects all versions. If a new PUT lands after the
listed snapshot, its new VersionId is not the target. Failed exact deletes, noncurrent versions,
legacy `null` versions, and delete markers are all re-enumerated by later passes.

The sweep logs the key, VersionId, and whether the target was a data version or marker. It never
logs credentials or presigned URLs.

### Other delete consumers

Every other production object delete continues to call the shared key convenience and therefore
deletes a captured exact-version set rather than issuing key-only `DeleteObject`. The sweep must
include image/report/build/snapshot garbage collection and compensation plus remote-libvirt console
parts. A source search for store-like `.delete(` calls and raw `delete_object(` calls is a release
gate; the only raw call may be `ObjectStore.delete_version`, and it must include `VersionId`.

## Failure and concurrency contract

| Position | Durable outcome | Recovery |
|---|---|---|
| version snapshot fails | database state unchanged | caller reports/counts failure; later pass retries |
| database re-check declines | no delete begins | the current owner remains authoritative |
| database commit fails | no delete begins | transaction rollback retains the prior state |
| crash after row retirement | captured and older versions survive | version orphan sweep enumerates them |
| delete response is ambiguous | only the named VersionId may have changed | retrying that VersionId is idempotent |
| later PUT races delete | later VersionId is outside the captured set | later bytes survive |
| exact current delete exposes an older version | older version becomes listable/current | same captured batch or later version sweep deletes it |
| Object Lock/per-version deny | target remains stored and pass reports failure | operator removes hold/deny; later pass retries |

No safety argument depends on a wall-clock lease, process liveness, PostgreSQL backend identity, ETag
delete preconditions, or cancellation of a boto3 thread.

## Rollout and rollback

1. Enable versioning on the external bucket and verify `Status=Enabled` without exclusions.
2. Grant the runtime role version inspection/list/delete permissions.
3. Roll out the new image. No database migration is involved.
4. Confirm readiness, then confirm a version-aware cleanup pass can remove a test version and marker.

Old replicas may coexist during step 3. Their lock-held key deletes create markers after versioning
is enabled; they do not permanently delete a later version, and new orphan sweeps enumerate the
markers. Rollback to the old image is data-safe but reduces cleanup to marker creation, so storage
grows until the version-aware image is restored. Suspending versioning is not a rollback action.

## Threat model

### Boundary inventory

Existing widened boundaries are the runtime-to-object-store calls for bucket inspection, version
listing, and permanent version deletion. Inputs are bucket/key names derived by KDIVE and service
replies controlled by the configured S3-compatible endpoint. The change adds no tenant-facing tool,
request field, secret, command, query, or path parser.

### Actors and trust

Authenticated tenants can cause uploads and lifecycle cleanup only through existing manifests,
artifact rows, write leases, and owner locks. The deployment operator controls endpoint, bucket,
credentials, versioning, Object Lock, and IAM policy. The configured object store is trusted to
implement the S3 versioning operations it advertises; malformed replies are treated as dependency
failure rather than trusted data.

### Controls

- Bucket state is validated before work; runtime credentials cannot enable external versioning.
- Exact-key filtering prevents a prefix such as `key` from selecting sibling `key-extra` versions.
- Every reply field is type-checked at the store boundary.
- Permanent deletion always includes a service-issued VersionId; tenant input never supplies one.
- Existing database fences and owner advisory locks decide reachability before cleanup.
- Page and per-root work bounds remain 1,000 listed entries and 200 examined versions.
- Errors expose operation, bucket/key, and VersionId only; standard secret/URL redaction still
  governs logs and operator output.

### Out of scope

Compromise of operator/object-store credentials, an administrator suspending versioning after
startup, an undeclared writer bypassing KDIVE publication fences, and stores that falsely claim S3
versioning compatibility are operator trust failures. Object Lock retention and lifecycle policies
remain operator-owned; KDIVE reports their refusal rather than bypassing them.

## Verification

Tests follow red-green-refactor and mutation-check every changed guard:

- unit contract tests prove missing/suspended versioning fails, enabled passes, malformed replies
  are categorized, `null` is accepted, exact-key filtering excludes siblings, pagination includes
  data versions plus markers, and no delete omits `VersionId`;
- focused cleanup tests inspect `pg_locks` from the delete callback and prove all three callbacks run
  with no transaction-scoped owner lock;
- race tests PUT a later version between snapshot and delete and prove its VersionId and bytes
  survive, while all captured versions and markers are removed;
- failure/crash tests leave a captured version behind and prove the next orphan sweep deletes it;
- the real MinIO fixture proves bucket validation, VersionId replies, legacy `null` handling where
  supported, marker/noncurrent enumeration, exact deletion, and test teardown of all versions;
- Compose/Helm render tests assert managed buckets enable versioning before app startup;
- the structural sweep rejects production key-only `DeleteObject` calls.

Focused Python tests run through `uv run python -m pytest`; repository gates are `just lint`,
`just type`, and `just ci`. Before completion, temporarily break each new safety guard, observe its
focused test fail for the intended assertion, restore it, and rerun green.
