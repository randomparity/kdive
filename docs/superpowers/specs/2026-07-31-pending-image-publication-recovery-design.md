# Pending Image Publication Recovery Design

## Goal

Make every expired `pending` image publication converge without reclaiming an object that a live
publisher is still writing. A complete object is registered only when it matches the catalog row;
an absent or invalid object and its row are reclaimed so private-image quota is released.

This design implements issue #1757 and [ADR-0526](../../adr/0526-fence-and-reconcile-pending-image-publications.md).

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

- PostgreSQL remains reachable to both publisher and reconciler when either acts. A disconnected
  publisher cannot retain a PostgreSQL session fence; its object is treated as abandoned once the
  backend releases the lock.
- The configured S3-compatible store honors the existing read-after-write and
  read-after-delete HEAD behavior on which KDIVE's current object lifecycle already relies.
- `image_catalog.digest` is `sha256:<hex>` and `size_bytes` is the expected qcow2 byte length for a
  pending reservation. Missing or malformed integrity evidence fails closed as invalid.
- The issue is one cohesive change. No new tool, setting, worker job, or operator procedure is
  introduced.

## Approaches

The selected approach is a database session advisory lock keyed by image row UUID. It is visible to
every service instance, survives transaction boundaries, and PostgreSQL releases it on backend
death. A row lock would require a long transaction during the PUT. A persisted heartbeat lease
would add a renewal deadline that can expire independently of a still-running store call. Both were
rejected in ADR-0526.

## Publication flow

`reserve_publish` continues to commit the quota-bearing `pending` row under the existing scope and,
for private images, PROJECT lock. It returns the row UUID, key, request digest, and expected size.

Before reading or writing source bytes, the publisher acquires the row's session advisory lock and
re-reads the row using the reservation identity (`id`, `state`, `object_key`, `digest`, and
`size_bytes`). If recovery or a newer attempt changed the row while the publisher waited, it raises
the existing `CONFLICT` error without writing.

While holding the fence, the publisher verifies source bytes, writes the qcow2 with a base64
`ChecksumSHA256`, HEAD-gates the result, performs the best-effort config write, and flips the row to
`registered`. The private-upload path keeps its registration audit in the same transaction as that
flip. The fence is released afterward in all ordinary success and exception paths; backend death is
the final release mechanism.

The PROJECT lock remains absent during the PUT. Tests distinguish the image fence from the project
quota lock rather than asserting that no advisory lock of any kind exists.

## Reconciliation flow

`repair_dangling_images` keeps the configured grace as the abandonment threshold. For each expired
non-`defined` candidate it opens a transaction and tries the candidate's publication fence without
waiting. If the lock is held, this pass skips the row. If acquired, it re-reads the candidate under
row lock and rechecks the deadline before any store call.

Registered candidates preserve current semantics: missing objects remove the row and present
objects remain untouched. Pending candidates use the following decision table:

| HEAD result | Integrity decision | Repair |
|---|---|---|
| absent | object never landed or was removed | delete row |
| present; size and SHA-256 match row | complete abandoned publication | set `registered` |
| present; size/checksum missing or mismatched | incomplete, overwritten, or unverifiable | delete object; confirm absent; delete row |
| store error, or object remains after delete | outcome is not proven | roll back/retain row and retry next pass |

The repair returns the number of rows that reached a terminal catalog outcome in that pass, whether
registered or removed. It logs the outcome without object bytes or tenant-sensitive provenance.

## Failure and retry behavior

- Death before the publisher acquires the fence leaves an ordinary reservation; recovery handles it
  after grace.
- Death during PUT releases the fence when PostgreSQL detects the dead backend. A missing, partial,
  or complete object then follows the decision table.
- Death after PUT but before registration leaves a checksum-bearing object; recovery registers it.
- A transient HEAD or delete failure preserves the pending row and its quota reservation for retry.
- Death after a successful delete but before row commit rolls the row deletion back. The next pass
  observes a missing object and deletes the row.
- A publisher that loses the reservation while waiting for the fence fails with `CONFLICT` before
  PUT, so it cannot recreate an object that recovery just removed.

## Data and interface changes

No schema migration is required; reserved migration 0093 remains unused. Existing row fields are
sufficient expected metadata. `ArtifactWriteRequest` gains an optional base64 SHA-256 value, and
the object-store adapter sends it only when present. Image qcow2 writes populate it; unrelated
writes retain their existing request shape.

The image sweep store port gains `head(key) -> HeadResult | None` in addition to delete/list
operations. Publication exposes a narrow fence helper shared by the publisher and repair, backed by
the existing advisory-lock infrastructure.

## Threat model

### Boundary inventory

- Existing boundary widened: publisher and reconciler coordinate through PostgreSQL advisory locks.
  The row UUID is service-generated and is the complete lock key input.
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
- A missing, malformed, or inconsistent checksum never registers.
- The shared lock orders publisher and repair; the row re-read prevents a stale candidate from
  acting on a re-armed or replaced reservation.
- Delete is followed by HEAD. The row is retained unless absence is observed.
- Store errors propagate to the per-repair failure boundary; no exception is swallowed as success.

### Out of scope

This change does not add S3 bucket versioning, retain version IDs, or redefine deletion across a
backend that can resurrect a key after a successful absence check. KDIVE's existing S3 lifecycle
already assumes current-key HEAD consistency. It also does not solve two independent publishers
targeting the same deterministic key; that catalog uniqueness problem remains separate from the
publisher-versus-reconciler fence.

## Tests

Focused service and reconciler tests must cover successful fence acquisition/release, stale
reservation rejection before PUT, death-after-write recovery, missing-object cleanup, valid
size/digest registration, size and checksum mismatch deletion, absent checksum deletion,
delete/HEAD failure retry, and private quota release after row removal.

An adversarial concurrency test suspends a real publisher inside its store PUT, ages the row beyond
grace, runs the real repair on another connection, and proves the pass skips it. A falsifier then
runs the same repair after the fence is released and proves the same row is recovered. Existing
quota concurrency tests continue to prove that the PROJECT lock is not held during the PUT.

Tests must be shown to bite by temporarily disabling the fence and integrity predicate, observing
the focused failures, and restoring the implementation before the final guardrails.
