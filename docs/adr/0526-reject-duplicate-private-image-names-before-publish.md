# 0526 — Reject duplicate private image names before publish

## Status

Accepted (2026-08-01)

Narrowly refines [ADR-0520](0520-quota-reservation-releases-the-project-lock-before-the-put.md)
§3 and [ADR-0525](0525-fence-and-reconcile-pending-image-publications.md) for the private finish:
PROJECT remains absent from object I/O, but registration reacquires it under IMAGE_PUBLISH.

## Context

Private image identity is unique by `(owner, provider, name)` once registered. Publication writes
the object before flipping its pending catalog row to registered. A second upload for an already
registered name therefore cannot register, but historically it reused the registered object's
deterministic key and overwrote live bytes before the uniqueness failure. ADR-0525 made publication
keys attempt-specific, so that overwrite is no longer possible, but the duplicate still performs a
potentially multi-gigabyte write before failing at registration.

The choice is whether `images.upload` replaces an existing private image or rejects the duplicate.
Replacement would have to define when existing Systems observe new bytes and how the old object is
retired without breaking a boot or materialization already in progress.

## Decision

`images.upload` rejects a project/provider/name that already has a registered private image. Under
the existing PROJECT advisory lock, the reservation phase checks for that registered identity
before quota accounting and before creating or adopting a pending row. It raises the existing
`CONFLICT` category through a dedicated service error type and names deletion plus a later upload
as the recovery path. The type lets the MCP handler attach that destructive recovery only to this
conflict, never to an in-flight attempt superseded by a concurrent winner. No catalog row and no
published object are written by the rejected attempt.

The check deliberately remains in the reservation transaction, after quarantine validation. The
PROJECT lock orders it with other private reservations without extending the lock across validation
or object-store I/O. Two first uploads that overlap may still share the pending-row adoption path.
ADR-0525's attempt fence ensures only the current reservation registers, while attempt-specific
keys isolate any write an earlier attempt already started. A later upload observes the registered
winner and is rejected before its publish write.

Private finish reacquires transaction-scoped PROJECT while the row's session-scoped IMAGE_PUBLISH
fence remains held, and keeps it through the registration flip and audit commit. The order is
therefore ``IMAGE_PUBLISH → PROJECT`` for this short finish only. It does not create a cycle with
reservation: a reservation holding PROJECT never attempts IMAGE_PUBLISH until after its transaction
commits, and a finisher acquires PROJECT before issuing the catalog update, so it holds no row lock
a reservation could await while retaining PROJECT. The object PUT completes before this co-hold.

This closes the duplicate precheck-to-reservation gap. If a second reservation reads the first row
as pending and pauses before adopting it, the first finisher waits for PROJECT. The second then
adopts and commits before the first may finish; the first receives the existing typed supersession
``CONFLICT``, and the current attempt registers under the same isolated-attempt object-key rules.
Registration can no longer overtake the decision whose result its competing reservation consumes.

Rejection does not supersede an object. The registered row, its object key and digest, and any
System already booted from that image remain unchanged. A caller that intends different bytes must
delete the existing image through `images.delete`, wait for deletion to complete, and then upload
again under the name.

## Consequences

- An upload rejected because the registered identity already exists may still pay quarantine read
  and guest-validation cost, but writes no published object and does not mutate the catalog entry.
- An overlapping first attempt may have written its isolated attempt-specific key before being
  superseded. It cannot register that key; ADR-0525's leaked-object recovery remains its owner.
- Private finish adds one bounded PROJECT section after the PUT. It contains only the fenced
  catalog flip and audit write; quota reservation remains the other bounded section.
- The public MCP contract gains a documented `CONFLICT` outcome and recovery sequence.
- Replacement remains an explicit delete-then-upload lifecycle, so no hidden object swap changes a
  running System or requires a second retirement mechanism.
- The check is private-upload-specific; public/operator publication and pending-attempt recovery
  keep their accepted behavior.

## Considered & rejected

- **Atomically replace the registered image.** This needs an image-version lifecycle, object
  retirement ownership, and a rule for Systems using the old object. Those semantics are not
  required to stop corruption and would turn one upload into a hidden destructive operation.
- **Rely only on attempt-specific object keys and fail at registration.** The registered image stays
  consistent, but a duplicate can upload a large rowless object before returning the inevitable
  uniqueness error. Rejecting at reservation gives an actionable typed failure before that write.
- **Keep deterministic keys and serialize writers.** Serialization prevents concurrent overwrite
  but a sequential duplicate still replaces bytes before the catalog uniqueness check, recreating
  the original defect.
