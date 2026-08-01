# 0531 — Investigation-scoped reusable kernel builds

## Status

Proposed

## Context

External kernel uploads are finalized onto one Run. Their artifact rows use
`owner_kind='runs'`, and the Run is the only durable handle for the validated kernel, optional
initrd, debuginfo, build id, cmdline, and provenance. A second System in the same Investigation
therefore needs another upload even when it should boot the identical build. Investigation close
already governs build-artifact reclamation, but the ownership model and `runs.create` contract do
not expose that lifetime.

A reusable build is a set, not one object: installation needs the validated kernel and may also
need an initrd and debuginfo, while debug consumers need the same build id and provenance. A bare
object checksum cannot identify that complete set.

## Decision

Finalizing an external build creates an immutable investigation-owned build record. Its public
`build_ref` is the SHA-256 of a versioned canonical document containing the validated artifact
checksums and build metadata. The artifact catalog rows use `owner_kind='investigations'` and the
Investigation id. A uniqueness constraint on `(investigation_id, build_ref)` makes concurrent or
replayed finalization converge on one record.

The record stores one exact validated artifact set. A finalizer first attempts to publish its
candidate record under the Investigation lock. On uniqueness conflict it reloads the winner and
verifies that the canonical document matches before using the winner's references for its source
Run. Only the winner registers investigation-owned artifact rows. A loser retains its uploaded
objects as uncommitted Run-prefix objects, deletes their exact versions after commit, and leaves a
failed delete to the existing prefix-orphan sweep. It never registers or deletes the winner's
objects. The lock spans catalog selection and database registration, so collection cannot race a
partially published winner.

`runs.complete_build` still completes its source Run and additionally returns the `build_ref`.
`runs.create` accepts an optional `build_ref`. Under the Investigation lock it resolves only a
record owned by the requested Investigation, requires its target architecture and build profile to
match the new Run, and creates that Run with the immutable build result and succeeded build step
already attached. No object is copied and no upload window is minted. The response and wrapper
contract direct the caller to `runs.install` rather than the external-build upload sequence.

The reference and normal create inputs participate in idempotency. A missing, malformed,
cross-Investigation, or incompatible reference fails as `configuration_error` without revealing
whether another tenant owns a matching build. The source Run may be terminal or deleted later;
the build record, not that Run, is the reuse authority.

Investigation-close-plus-grace garbage collection deletes the build's investigation-owned artifacts
and then its build record. There is no open-Investigation TTL: the promised lifetime lasts until
the Investigation closes and its configured grace deadline passes. Reclaim locks the Investigation
and rechecks that no live Run references the build before deletion. Runs store the selected
`build_ref`, so concurrent create versus reclaim is serialized and the reference remains auditable.

## Consequences

- One validated upload can back any number of compatible Runs and Systems in its Investigation.
- The public handle identifies the complete build set; callers do not assemble object references.
- Cross-Investigation reuse is rejected at the ownership predicate even when bytes are identical.
- Existing run-owned build rows remain readable and reclaimable; there is no backfill or dual
  creation path for new completions.
- Duplicate physical uploads can occur before the content identity is known. Only one becomes the
  durable build; losing object versions are best-effort deleted and remain covered by orphan repair.
- Reuse bypasses build upload and validation because it selects an already validated immutable
  record. Install, boot, and debug behavior remain unchanged.
- The schema gains an investigation-build catalog and a nullable `runs.build_ref` audit link.

## Considered & rejected

- **Pass a source Run id to `runs.create`.** Rejected because Run lifetime would remain the
  ownership authority and a mutable lifecycle object is not a content address for the build set.
- **Pass individual kernel, initrd, and debuginfo checksums.** Rejected because it lets callers
  compose a combination KDIVE never validated and omits build id, cmdline, and provenance.
- **Add `runs.reuse_build`.** Rejected because it adds a second post-create build transition and
  races System binding; create-time selection is atomic with admission and idempotency.
- **Widen `runs.install`.** Rejected because install should consume the Run's immutable build
  result, not mutate build identity while operating on a System.
- **Keep per-Run uploads.** Rejected because it does not satisfy upload-once reuse across Systems.
