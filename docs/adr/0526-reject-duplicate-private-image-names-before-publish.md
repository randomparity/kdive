# 0526 — Reject duplicate private image names before publish

## Status

Accepted (2026-08-01)

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

Rejection does not supersede an object. The registered row, its object key and digest, and any
System already booted from that image remain unchanged. A caller that intends different bytes must
delete the existing image through `images.delete`, wait for deletion to complete, and then upload
again under the name.

## Consequences

- An upload rejected because the registered identity already exists may still pay quarantine read
  and guest-validation cost, but writes no published object and does not mutate the catalog entry.
- An overlapping first attempt may have written its isolated attempt-specific key before being
  superseded. It cannot register that key; ADR-0525's leaked-object recovery remains its owner.
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
