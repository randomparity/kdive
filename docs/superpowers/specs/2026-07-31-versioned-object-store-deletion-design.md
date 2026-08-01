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

The configured bucket must report `Status=Enabled` from `GetBucketVersioning`. Missing status and
`Suspended` fail with `configuration_error`; the message names the bucket, observed state, required
state, and recovery action. The check runs before server, worker, or reconciler work begins and
remains part of object-store readiness.

The standard response exposes only bucket status and MFA Delete. It cannot report MinIO's
non-standard excluded-prefix configuration, so KDIVE does not claim to fail fast on that state.
External endpoints are supported only when `Enabled` applies bucket-wide; their operator verifies
that provider-specific prerequisite. Managed MinIO is enabled without exclusions. Prefix exclusion
under any KDIVE key is a contract violation because a mutable `null` identity can make a delayed
delete unsafe.

Compose `minio-init`, the Helm bundled-backend initializer, and the live MinIO test fixture enable
versioning immediately after bucket creation. External deployments enable it outside KDIVE before
rolling out the image. KDIVE does not call `PutBucketVersioning` for an external bucket.

The documented runtime policy adds `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion`. Existing read, write, current-key list, and HEAD permissions remain. The
first rollout installs and verifies these grants before versioning is enabled or an application is
started.

### Store values and operations

Add an immutable `ObjectVersion` value carrying:

- `key: str`;
- `version_id: str` (including the literal `null` legacy identifier);
- `last_modified: datetime`;
- `etag: str | None` (`None` for a delete marker);
- `is_latest: bool`;
- `is_delete_marker: bool`.

`StoredArtifact` and `HeadResult` also carry the observed `version_id`. This value is ephemeral: it
lets compensation delete the same identity that a PUT or ETag/row-fenced HEAD selected, but it is
not added to an artifact row or public response.

`iter_prefix_version_pages(prefix)` lazily paginates `ListObjectVersions` with the existing
1,000-entry page bound. It validates fields through the store boundary and combines `Versions` and
`DeleteMarkers` into a deterministic key/mtime/VersionId order; boto3 exposes those collections
separately, so no cross-collection service order is claimed.

`capture_exact_versions(key, limit)` retains only exact-key entries and returns a `VersionBatch`
containing at most positive integer `limit` targets plus `history_complete: bool`. The unit is
object versions/delete markers, the scope is one capture call, and there is no clock. Reaching the
limit leaves `history_complete=False`; deletion may remove captured nonlatest targets but must leave
the captured latest target. Recovery is another capture in a later cleanup pass. Peak memory is one
service page plus `limit` targets.

`delete_version(key, version_id)` always supplies `VersionId` to `DeleteObject`. Deleting an absent
version is idempotent. `delete_batch(batch)` deletes nonlatest targets first. It attempts the
captured latest target only when `history_complete=True` and every nonlatest delete succeeded. A
malformed reply or client/transport fault maps to `infrastructure_failure` and names the key and
VersionId without exposing credentials.

There is no `delete(key)` operation. Identity-sensitive callers pass the VersionId selected by
their PUT or HEAD directly to `delete_version`. A permanently retired key may loop over bounded
capture/delete batches; incomplete batches make progress only on nonlatest entries and keep the
current data or marker visible. `ListObjectVersions` is not an atomic snapshot, so a caller may use
key-history batches only after its lifecycle/database fences prove no publisher can make that key
live again.

Current-object APIs (`head`, `get`, `list_prefix`) retain their logical-key behavior. This avoids
leaking versioning concerns into readers and callers that do not delete.

### Rootfs reclaim

Before opening its reclaim transaction, rootfs reclaim captures at most one 1,000-target exact-key
batch. Under `LockScope.INVESTIGATION`, it re-reads the due row,
referencer/fetch-lease/staging-partial fences, unlinks the staged base, and removes the artifact row.
It commits before deleting any captured version.

It then calls `delete_batch` without an open transaction or advisory lock. An incomplete batch or a
failed/ambiguous prerequisite delete leaves the latest target; the artifact row stays retired and
every surviving version is discoverable under `local/investigations/` by the version-aware orphan
sweep. There is no application timeout around an uncancellable blocking thread.

A later PUT to the same key is a different VersionId and cannot be hit by the captured deletes. A
fetch that began first still has a durable lease and blocks row retirement; one that begins later
cannot resolve the removed row.

### Expired-upload reaper

The reaper claims and removes the expired manifest under the existing owner lock without listing or
deleting from the store in that transaction. After commit it enumerates the prefix's version pages,
groups entries by exact key, and captures bounded key batches. For each key, a short owner-locked
transaction re-runs `owner_key_is_fenced`; it then releases the transaction and calls
`delete_batch`. Contention or a new artifact, manifest, or live write lease declines the key for a
later pass. An incomplete batch retains latest and leaves the remainder to the orphan sweep.

Store/list/delete failures count as undeleted. Since the manifest is already gone, surviving
versions remain candidates for the version-aware orphan sweep.

### Upload-orphan sweep

The sweep walks `local/runs/` and `local/investigations/` through
`iter_prefix_version_pages`. Its existing 200-unit per-root budget now counts captured versions and
markers. The reference clock is PostgreSQL `now()` for the unchanged grace deadline; exhausting the
per-pass/per-root budget stops listing and leaves every unexamined version for the next reconciler
pass. It attributes entries by key, deduplicates a key across page boundaries within the bounded
work set, and captures at most that root's remaining budget. Immutable version mtimes replace the
current-key HEAD rewrite check.

For a candidate key it captures a bounded batch, then attempts the owner lock and re-runs the
existing key-scoped artifact, manifest, write-lease, and grace fences. The transaction ends before
exact deletion begins. A key with any live database reference conservatively protects all versions.
`delete_batch` removes nonlatest entries first and attempts latest only for a complete history. A
new PUT after capture has a different VersionId. Failed exact deletes, incomplete histories,
noncurrent versions, legacy `null` versions, and delete markers are all re-enumerated later.

The sweep logs the key, VersionId, and whether the target was a data version or marker. It never
logs credentials or presigned URLs.

### Other delete consumers

Remove the shared key-only `delete` surface. Identity-sensitive consumers use the VersionId selected
by the same observation that licenses cleanup:

- `discard_unregistered_objects` compares the current HEAD to the `StoredArtifact` written by this
  attempt, rechecks row absence, and deletes that stored VersionId;
- external-build chunk cleanup deletes the VersionIds captured by its validated HEAD results; and
- leaked-image repair HEADs before its final row-absence query and deletes that HEAD's VersionId.

Consumers whose lifecycle decision permanently retires the key use bounded capture/delete batches:

- report, closed-investigation, and expired-build artifact garbage collection;
- expired private-image object/config retirement;
- System console-part, SysRq, and rotation-sidecar teardown;
- remote-libvirt sealed console-part retirement; and
- the three #1751 paths described above.

These callers remove their database row only after a complete retired-key purge, except the #1751
row-first paths whose version-aware orphan sweep is the durable continuation. A source search for
store-like `.delete(` calls and raw `delete_object(` calls is a release gate; the only raw call may
be `ObjectStore.delete_version`, and it must include `VersionId`.

## Failure and concurrency contract

| Position | Durable outcome | Recovery |
|---|---|---|
| bounded version capture fails | database state unchanged | caller reports/counts failure; later pass retries |
| capture reaches its version budget | latest target is retained | later pass captures the remaining history |
| database re-check declines | no delete begins | the current owner remains authoritative |
| database commit fails | no delete begins | transaction rollback retains the prior state |
| crash after row retirement | captured and older versions survive | version orphan sweep enumerates them |
| delete response is ambiguous | only the named VersionId may have changed | retrying that VersionId is idempotent |
| peer PUT follows an identity observation | exact delete still names the observed VersionId | peer bytes survive |
| nonlatest exact delete fails | captured latest remains current | later sweep retries without resurrection |
| latest exact delete is ambiguous | all captured older versions are already absent | retry latest; no older captured bytes can reappear |
| Object Lock/per-version deny | target remains stored and pass reports failure | operator removes hold/deny; later pass retries |

No safety argument depends on a wall-clock lease, process liveness, PostgreSQL backend identity, ETag
delete preconditions, or cancellation of a boto3 thread.

## Rollout and rollback

1. Quiesce and stop every old server, worker, and reconciler so no KDIVE PUT or DELETE is in flight.
2. Grant and verify version inspection, listing, and exact-delete permissions.
3. Enable versioning; verify `Status=Enabled` and the provider-specific no-exclusions prerequisite.
4. Wait the provider's activation barrier before any application write. Amazon S3's documented
   first-enable propagation interval applies; managed MinIO initialization waits for `mc version
   enable` and verifies status before completing.
5. Start only the version-aware image and confirm readiness plus exact version/marker cleanup.

This first adoption is a documented stop-old-first downtime release under ADR-0088. A pre-ADR image
must not coexist with or replace the new image while accepting work: its key-only cleanup creates
markers and can hide concurrent writes. An emergency diagnostic rollback must remain quiesced;
service recovery is a forward deployment of an ADR-0524-aware image. Suspending versioning is not a
rollback action. Later releases that implement this contract may use normal rolling upgrades.

## Threat model

### Boundary inventory

Existing widened boundaries are the runtime-to-object-store calls for bucket inspection, version
listing, and permanent version deletion. Inputs are bucket/key names derived by KDIVE and service
replies controlled by the configured S3-compatible endpoint. The change adds no tenant-facing tool,
request field, secret, command, query, or path parser.

### Actors and trust

Authenticated tenants can cause uploads and lifecycle cleanup only through existing manifests,
artifact rows, write leases, and owner locks. The deployment operator controls endpoint, bucket,
credentials, bucket-wide versioning, Object Lock, provider-specific exclusions, and IAM policy. The
configured object store is trusted to implement the S3 versioning operations it advertises and to
apply `Enabled` to every KDIVE prefix; malformed replies are treated as dependency failure rather
than trusted data.

### Controls

- Standard bucket state is validated before work; runtime credentials cannot enable external
  versioning. Provider-specific no-exclusion verification remains an operator prerequisite because
  the standard API does not expose it.
- Exact-key filtering prevents a prefix such as `key` from selecting sibling `key-extra` versions.
- Every reply field is type-checked at the store boundary.
- Permanent deletion always includes a service-issued VersionId; tenant input never supplies one.
- Existing database fences and owner advisory locks decide reachability before cleanup.
- Page and per-root work bounds remain 1,000 listed entries and 200 examined versions.
- Errors expose operation, bucket/key, and VersionId only; standard secret/URL redaction still
  governs logs and operator output.

### Out of scope

Compromise of operator/object-store credentials, an administrator suspending versioning or adding
an excluded prefix after validation, an undeclared writer bypassing KDIVE publication fences, and
stores that falsely claim S3 versioning compatibility are operator trust failures. Object Lock
retention and lifecycle policies remain operator-owned; KDIVE reports their refusal rather than
bypassing them.

## Verification

Tests follow red-green-refactor and mutation-check every changed guard:

- unit contract tests prove missing/suspended versioning fails, enabled passes, malformed replies
  are categorized, `null` is accepted, exact-key filtering excludes siblings, pagination includes
  data versions plus markers, and no delete omits `VersionId`;
- focused cleanup tests inspect `pg_locks` from the delete callback and prove all three callbacks run
  with no transaction-scoped owner lock;
- identity-race tests PUT a peer version after the first attempt's PUT/HEAD and row probe, then prove
  exact deletion removes only the first VersionId;
- failure-order tests split one key's history across pages, fail immediately before the latest
  deletion, and prove the same latest data/marker remains current;
- budget tests give one key more versions than both a page and the per-root pass allowance, prove
  memory/work remain bounded, and prove repeated passes converge without deleting latest early;
- failure/crash tests leave a captured version behind and prove the next orphan sweep deletes it;
- the real MinIO fixture proves bucket validation, VersionId replies, legacy `null` handling where
  supported, marker/noncurrent enumeration, exact deletion, and test teardown of all versions;
- Compose/Helm render tests assert managed buckets enable and verify versioning before app startup;
- rollout documentation tests/guards keep the permission-before-enable, quiesce, and activation
  barrier explicit, including the provider-specific exclusion limitation;
- the structural sweep rejects production key-only `DeleteObject` calls.

Focused Python tests run through `uv run python -m pytest`; repository gates are `just lint`,
`just type`, and `just ci`. Before completion, temporarily break each new safety guard, observe its
focused test fail for the intended assertion, restore it, and rerun green.
