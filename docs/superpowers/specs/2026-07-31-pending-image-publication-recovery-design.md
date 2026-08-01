# Pending Image Publication Recovery Design

## Goal

Make every expired attempt-aware `pending` image publication converge without reclaiming an object
that a live publisher is still writing. A complete object is registered only when it matches the
catalog row; an absent or invalid object and its row are reclaimed so private-image quota is
released. Legacy null-attempt rows remain untouched during mixed-version operation.

This design implements campaign phase #1789 of #1757 and
[ADR-0525](../../adr/0525-fence-and-reconcile-pending-image-publications.md).

## Current failure

Publication reserves a row, writes a deterministic object key, then registers the row. A process
death between the PUT and registration leaves a durable `pending` row. The dangling-image repair
only removes rows whose object is absent, while the leaked-image repair protects every referenced
object, so the pair survives forever. A private row continues to count toward both private-image
quota limits.

The repair's `pending_since + grace` deadline also does not describe a live transfer. An in-bounds
image can take longer than the grace period, after which the current repair may delete its row while
the writer is still active.

## Assumptions

- A publisher with a live PostgreSQL session is protected as active. A disconnected publisher is a
  failed attempt even if its blocking store thread continues; attempt-specific keys make the late
  PUT harmless to catalog state and eligible for the existing leaked-object sweep.
- Async task cancellation is also a failed attempt even if `asyncio.to_thread` continues the PUT.
  The same attempt-key isolation applies after normal context unwinding releases the session lock.
- The configured S3-compatible store honors the existing read-after-write and
  read-after-delete HEAD behavior on which KDIVE's current object lifecycle already relies.
- `image_catalog.digest` is `sha256:<hex>` and `size_bytes` is the expected qcow2 byte length for a
  pending reservation. New requests validate that form before reservation. A malformed
  attempt-aware row
  is terminally unverifiable, not a retryable store failure: recovery deletes its object, confirms
  absence, and removes the row.
- The issue is one cohesive change. No new tool, setting, worker job, or operator procedure is
  introduced.

## Approaches

The selected approach combines a database session advisory lock keyed by image row UUID with a
persisted attempt UUID in every new-writer pending reservation. The lock is visible to every service instance,
survives transaction boundaries, and PostgreSQL releases it on backend death. The attempt UUID
keeps a late PUT isolated if the database session dies before its blocking store thread. A row lock
would require a long transaction during the PUT. A persisted heartbeat lease would add a renewal
deadline that can expire independently of a still-running store call. Both were rejected in
ADR-0525.

## Publication flow

`reserve_publish` continues to commit the quota-bearing `pending` row under the existing scope and,
for private images, PROJECT lock. Each reservation/adoption mints a new attempt UUID and persists
it and the attempt-specific qcow2/config keys. A private insert/adoption replaces
`publication_principal` with the current authenticated principal in that transaction; a public
reservation writes `NULL`. It returns the row UUID, attempt UUID, keys, request digest, and expected
size.

Before reading or writing source bytes, the publisher acquires the row's session advisory lock.
The session and reconciler transaction helpers both use the same
`LockScope.IMAGE_PUBLISH`-derived bigint; the separately salted leadership-lock helper is not used.
The publisher re-reads the row using the reservation identity (`id`, `publication_attempt_id`,
`state`, `object_key`, `digest`, and `size_bytes`) in a short committed transaction. It proves the
connection returned to transaction-idle before writing. If recovery or a newer attempt changed the
row while the publisher waited, it raises the existing `CONFLICT` error without writing.

While holding the fence, the publisher verifies source bytes, writes the qcow2 with a base64
`ChecksumSHA256`, HEAD-gates the result, performs the best-effort config write, and flips the row to
`registered`. The private-upload path keeps its registration audit in the same transaction as that
flip. The flip clears the attempt/principal fields. The fence is released afterward in all ordinary
success and exception paths; backend death is the final release mechanism. If the backend dies
while the PUT thread continues, the attempt key cannot collide with recovery or a later attempt.

The PROJECT lock remains absent during the PUT. Tests distinguish the image fence from the project
quota lock rather than asserting that no advisory lock of any kind exists.

### Predecessor-writer compatibility

The phase-1 predecessor can adopt a pending row but does not write `publication_attempt_id` or take
the image fence. Migration 0092 therefore installs a trigger that recognizes predecessor-shaped
mutations: while `state` remains `pending`, changing any publication-owned field while preserving
the same non-null attempt UUID clears both attempt and principal. A new writer changes the attempt
UUID in the same statement, so its reservation remains attempt-aware. If an update transitions out
of `pending` while preserving the same non-null attempt, the trigger raises a stable database
exception: this is a late predecessor registration that no longer owns the row. Failing the
statement prevents the old caller from treating a returned pending row as successful or committing
a private registration audit. New registration explicitly clears both fields and is allowed.

A `BEFORE DELETE` compatibility trigger returns `NULL` for a pending row whose attempt is non-null,
so predecessor dangling repair, private expiry, and inventory prune cannot remove a new writer's
active reservation without the fence. New recovery, after acquiring the fence and proving a delete
outcome, clears attempt/principal inside the same locked transaction before issuing its delete.

Thus an old writer adopting a phase-2 reservation atomically turns it into legacy null-attempt
state before its unfenced PUT; recovery skips the row throughout coexistence. Conversely, if an old
writer reserved first and a phase-2 writer then adopts the row, the late old registration fails and
cannot register or audit the successor's key. This trigger is compatibility machinery for the expand phase,
not the final invariant; issue #1790 may remove or replace it when old writers leave the supported
coexistence horizon.

Both private-expiry predicates exclude `pending` state: the candidate SELECT and the later
`FOR UPDATE` re-read immediately before object/row deletion. The second predicate closes a selected
`registered` candidate becoming an adopted `pending` reservation before the lock is obtained. The
pending-publication repair is the only automatic deletion path for a reservation and therefore the
only path that needs its fence. If it recovers an already-expired object to `registered`, normal
expiry removes it on the next pass.

Config inventory follows the same lifecycle ownership. Its config-only columns are `format`,
`root_device`, `visibility`, `capabilities`, and `description`; those update independently. The
CAS-protected set is `object_key`, `kernel_config_key`, `volume`, `path`, `digest`, `provenance`,
`provenance_attested`, `state`, `size_bytes`, `pending_since`, `publication_attempt_id`, and
`publication_principal`. Inventory has no reason to change the final four, and preserving them is an
explicit invariant rather than an omission from its SET list.

Runtime realization fields update only when the loaded snapshot was not `pending` and a SQL
compare-and-swap confirms the current `(state, publication_attempt_id)` still matches that snapshot.
A miss preserves the whole protected set and defers recomputation to the next pass. Config-only
changes still count in `diff.updated`; a failed runtime CAS does not. When a desired runtime change
was skipped by the CAS, the pass appends one `diff.warned` record saying publication state changed
and realization was deferred. This is two-sided: neither a `defined -> pending` reservation nor a
`pending -> registered` finish can be overwritten by a stale inventory snapshot. Config prune
re-reads under row lock and returns a no-op for `pending`. After recovery registers or deletes the
attempt, later inventory passes resume normal ownership.

## Reconciliation flow

`repair_dangling_images` keeps the configured grace as the abandonment threshold. It skips legacy
pending rows whose attempt is null. For each other expired non-`defined` candidate it opens a
transaction and tries the candidate's publication fence without
waiting. If the lock is held, this pass skips the row. If acquired, it re-reads the candidate under
row lock and rechecks the deadline before any store call. That same transaction, xact advisory lock,
and row lock remain open through every HEAD/delete call, terminal row mutation or private audit, and
commit. Recovery never commits the fence before acting on the store.

Registered candidates preserve current semantics: missing objects remove the row and present
objects remain untouched. Pending candidates use the following decision table:

| HEAD result | Integrity decision | Repair |
|---|---|---|
| absent | object never landed or was removed | delete row |
| present; size and SHA-256 match row | complete abandoned publication | reconcile config key and set `registered`; atomically audit private recovery under persisted principal |
| present; size/checksum missing or mismatched | incomplete, overwritten, or unverifiable | delete object; confirm absent; delete row |
| persisted digest malformed | attempt-aware but unverifiable reservation | delete any object; confirm absent; delete row |
| store error, or object remains after delete | outcome is not proven | roll back/retain row and retry next pass |

The repair returns the number of rows that reached a terminal catalog outcome in that pass, whether
registered or removed. It logs the outcome without object bytes or tenant-sensitive provenance.

## Failure and retry behavior

- Death before the publisher acquires the fence leaves an ordinary reservation; recovery handles it
  after grace.
- Death during PUT releases the fence when PostgreSQL detects the dead backend. A missing, partial,
  or complete object then follows the decision table. A PUT that lands later uses the abandoned
  attempt's unique key and becomes a bounded leaked-object candidate, never the new row's object.
- Async cancellation has the same safe failed-attempt outcome. It need not pretend the offloaded
  SDK thread was cancelled; a late object remains isolated under the abandoned attempt key.
- Death after PUT but before registration leaves a checksum-bearing object; recovery registers it.
- A typed HEAD or delete failure preserves the pending row and its quota reservation for retry,
  reports candidate context, and does not prevent later candidates from progressing in the pass.
- Death after a successful delete but before row commit rolls the row deletion back. The next pass
  observes a missing object and deletes the row.
- A publisher that loses the reservation while waiting for the fence fails with `CONFLICT` before
  PUT, so it cannot recreate an object that recovery just removed.
- Recovery HEADs a persisted config key before registering a valid qcow2. Presence retains the
  offer, absence clears it, and a store error retains the row for retry.
- Valid private recovery emits the existing registration audit under the persisted initiating
  principal in the same transaction as the flip. Missing principal state fails closed to reclaim.
- Adoption by another principal replaces the prior actor before any write, so recovery attributes
  the current attempt rather than the abandoned one.
- Inventory reconcile and declaration removal cannot overwrite or prune a pending reservation,
  including when their candidate snapshot predates the reservation commit.

## Data and interface changes

Migration 0092 adds nullable `publication_attempt_id uuid` and `publication_principal text` columns
without backfill or a final constraint. New writers populate attempts; old-image and pre-migration
pending rows remain nullable legacy state that recovery ignores in this phase. Registration clears
both fields. A compatibility trigger demotes predecessor-shaped pending mutations to null-attempt
legacy state, fails predecessor-shaped registration of a non-null successor attempt, and allows
new registration only when it explicitly clears the fields. A delete trigger protects
attempt-aware pending rows; fenced recovery explicitly disarms it before terminal deletion.
`ImageCatalogEntry` gains both nullable fields so its
`extra="forbid"` validation continues to accept `SELECT *`; `PublishReservation` gains the required
attempt UUID. Every explicit image projection is audited and extended when it feeds that model.
Catalog/MCP response builders remain explicit projections and must never render either internal
publication field, especially `publication_principal`. Migrated-database finish, resolve, list, and
describe paths are regression-tested.

Publish object keys include the attempt UUID; consumers already read persisted keys and need no
derivation change. The deterministic `config_object_key` remains for inventory/staged capture, while
an attempt-aware helper produces both publish keys.

`ArtifactWriteRequest` gains `sha256_b64: str | None = None`, matching
`ArtifactStreamRequest.sha256_b64`. A single helper accepts only `sha256:` plus exactly 64 hex
digits, converts the 32 digest bytes to standard padded base64, and raises
`CONFIGURATION_ERROR` for malformed input digests before a reservation is committed. Recovery calls
a non-raising parse form: a malformed persisted digest selects the invalid-object terminal branch,
not the retry/error branch. `ObjectStore.put_artifact` passes a non-null value as the SDK's
`ChecksumSHA256` and omits the argument otherwise. The normal publish HEAD gate and recovery both
require `size_bytes == reservation.size_bytes` and `checksum_sha256 == expected padded base64`;
presence-only or missing checksum evidence never registers a qcow2. Config siblings retain their
best-effort presence-only HEAD contract.

The image sweep store port gains `head(key) -> HeadResult | None` in addition to delete/list
operations. Publication exposes a narrow fence helper shared by the publisher and repair, backed by
new session/xact variants over one image-publication lock key.

## Threat model

### Boundary inventory

- Existing boundary widened: publisher and reconciler coordinate through PostgreSQL advisory locks.
  The row UUID is service-generated and is the complete shared lock key input.
- Existing boundary widened: the reconciler consumes S3 HEAD checksum, size, and presence data to
  decide registration or deletion.
- Existing destructive boundary: the reconciler deletes an object and catalog row. It may act only
  after the grace, a successful nonblocking fence acquisition, and a locked row re-read.

### Actors and trust

Authenticated tenants can supply private image bytes but cannot choose object keys, row UUIDs, or
persisted digests after reservation. KDIVE services and PostgreSQL are trusted to enforce row scope
and locks. The S3-compatible backend is trusted for the same integrity and HEAD visibility contract
already required by artifact storage; its replies are still treated as fallible inputs.

### Controls

- Key construction retains existing component validation and project scoping.
- Registration requires both persisted size and SHA-256 to match HEAD evidence.
- Attempt-specific object keys prevent a disconnected or superseded writer from recreating the
  object key recovery or a later attempt owns.
- A missing, malformed, or inconsistent checksum never registers.
- The shared lock orders publisher and repair; the row re-read prevents a stale candidate from
  acting on a re-armed or replaced reservation.
- Delete is followed by HEAD. The row is retained unless absence is observed.
- Typed store errors stop only the current candidate, preserve its row, and are reported with
  candidate context; later candidates continue. No exception is swallowed as success.

### Out of scope

This change does not add S3 bucket versioning, retain version IDs, or redefine deletion across a
backend that can resurrect a key after a successful absence check. KDIVE's existing S3 lifecycle
already assumes current-key HEAD consistency. It also does not solve two independent publishers
targeting the same deterministic key; that catalog uniqueness problem remains separate from the
publisher-versus-reconciler fence.

## Tests

The focused acceptance matrix is binding:

| Contract branch | Required observable proof |
|---|---|
| shared fence | a real session holder makes the reconciler's exact xact try-lock return false; after release the same key succeeds |
| transaction boundary | both worker-autocommit and MCP-non-autocommit shapes enter PUT transaction-idle with PROJECT lock absent and IMAGE_PUBLISH held |
| active slow publisher | a blocked PUT with an expired pending row survives the real dangling repair; the same pass repairs it after fence release |
| stale reservation | recovery wins before fence acquisition, publisher raises `CONFLICT`, and no PUT occurs |
| recovery fence lifetime | recovery pauses after acquiring xact+row locks; a publisher blocks before revalidation/PUT until terminal commit, then conflicts without PUT |
| session loss/cancellation | a late PUT lands only under the abandoned attempt key and cannot alter a successor/recovered row |
| attempt adoption | every adoption changes both qcow2/config keys; cross-principal private adoption persists the second actor |
| valid abandoned object | matching size/canonical checksum registers and increments the repair terminal-outcome count |
| config sibling | present retains the key, absent clears it, and HEAD failure preserves pending state for retry |
| private audit | recovered registration and the existing audit transition commit atomically under the persisted principal; injected audit failure registers nothing |
| missing private principal | valid bytes are deleted and the row reclaimed, never registered |
| invalid object | wrong size, missing/malformed checksum, or mismatched checksum deletes; a still-present post-delete HEAD preserves the row |
| malformed digest | a new request fails before reservation; a seeded attempt-aware pending row deletes object+row and releases quota |
| crash after delete | rollback/death after confirmed object deletion preserves the row; the next pass sees missing and removes it |
| private expiry | both candidate and locked predicates skip pending; an already-expired recovered row registers first and prunes only on the next TTL pass |
| inventory races | `defined -> pending` and `pending -> registered` interleavings preserve every protected column and report deferred realization accurately |
| inventory removal | declaration removal during blocked PUT neither prunes nor cordons the pending row |
| registered regression | present registered rows remain; missing registered rows retain existing deadline removal semantics |
| quota release | before/after usage asserts both pending+registered count and summed `size_bytes`; reclaimed row releases both caps |
| schema/read model | migrated finish, resolve, list, and describe accept the new columns and expose neither internal field |
| migration compatibility | pre-0092 pending rows remain null-attempt legacy state, new-writer pending rows receive distinct non-null attempts, and both new registration paths clear attempt/principal atomically |
| predecessor adoption | phase-1 SQL adopting a phase-2 pending row atomically clears attempt/principal before its unfenced PUT; recovery skips the demoted row |
| predecessor registration | phase 1 reserves, phase 2 adopts, then late phase-1 public and private finish callers fail; no success or audit commits and the phase-2 attempt remains pending and unchanged |
| predecessor deletion | phase-1 dangling, expiry, and inventory-prune DELETE shapes cannot remove an attempt-aware pending row; fenced new recovery can disarm and delete it atomically |
| candidate isolation | a typed store failure on the first candidate preserves it while a healthy later candidate reaches a terminal outcome in the same pass |
| exact SDK checksum | the request maps canonical padded base64 to `ChecksumSHA256`; null omits it |
| normal HEAD integrity | matching size+checksum registers; absent, wrong-size, missing/malformed, or mismatched checksum stays pending and raises the typed publish failure |

Every terminal recovery arm asserts the repair return count; every retry/no-op arm asserts zero.

An adversarial concurrency test suspends a real publisher inside its store PUT, ages the row beyond
grace, runs the real repair on another connection, and proves the pass skips it. A falsifier then
runs the same repair after the fence is released and proves the same row is recovered. Existing
quota concurrency tests continue to prove that the PROJECT lock is not held during the PUT.

Tests must be shown to bite by temporarily disabling the fence and integrity predicate, observing
the focused failures, and restoring the implementation before the final guardrails. The
private-expiry locked predicate and inventory CAS each get the same bite check because candidate-only
and one-sided implementations are plausible regressions that simpler tests would miss. The normal
publish HEAD test must also redden when its predicate is temporarily weakened to presence-only.
