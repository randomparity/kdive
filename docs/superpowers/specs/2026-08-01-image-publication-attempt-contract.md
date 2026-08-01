# Image Publication Attempt Contract

## Scope

Issue #1790 completes ADR-0525's expand-contract rollout after the attempt-aware phase-two image
became the supported predecessor. Migration 0093 normalizes legacy pending rows, activates their
existing integrity-based recovery path, and makes publication-attempt state a database invariant.
It introduces no new publication behavior or architecture decision.

## Migration contract

Migration 0093 assigns a fresh `publication_attempt_id` to every remaining `pending` row whose
attempt is null. It does not invent an initiating principal: public publication has no principal,
and a legacy private row lacks trustworthy attribution. Existing recovery therefore registers a
valid public object, while a private row without a principal follows ADR-0525's fail-closed reclaim
path and releases its quota.

The migration replaces the expand-phase compatibility triggers. The final check requires exactly
these invariants:

- `pending` rows have a non-null `publication_attempt_id`;
- non-pending rows have a null `publication_attempt_id`;
- `publication_principal`, when present, belongs only to a private `pending` row.

The phase-two image remains the rollback-compatible predecessor. Its reservations already write an
attempt, and its registration updates state while clearing attempt and principal atomically. Its
recovery deletion first clears publication state while the row is still pending, however. A narrow
`BEFORE UPDATE` compatibility trigger converts that predecessor-shaped disarm into the terminal row
deletion and suppresses the update, so the constraint is never transiently false. Phase-three code
deletes a fenced pending row directly and does not depend on this compatibility path.

## Recovery activation

The reconciler candidate query no longer excludes attempt-less pending rows because migration 0093
makes that state impossible. Each normalized row enters the existing ADR-0525 decision table under
the same advisory fence and row lock. Valid checksum-and-size-matching public objects register;
missing, malformed, mismatched, or unattributable private objects are removed with their catalog
reservation. Candidate-level typed store failure isolation remains unchanged.

## Compatibility proofs

Database tests apply migrations only through 0092, seed predecessor-valid legacy rows, and then
apply 0093. They prove normalization and the final constraint. Separate tests issue the exact
phase-two SQL shapes against the final schema: reserve/adopt, register, predecessor recovery
disarm/delete, and ordinary registered-row deletion. Recovery tests prove normalized legacy public
registration and legacy private quota release.

## Failure and rollback behavior

Migration 0093 is transactional. A failed normalization or constraint installation rolls back as a
unit. After it commits, image-only rollback to phase two remains viable through the compatibility
proofs above. Rollback to the pre-attempt phase-one image is outside this contract because phase two
is now the supported predecessor, as authorized by the campaign sequencing.

## Threat model

No trust boundary is added. The existing destructive reconciler boundary is activated for legacy
rows only after the migration gives them a database-visible attempt identity. PostgreSQL-generated
UUIDs cannot be selected by tenants, the existing advisory fence controls recovery, and the
ADR-0525 integrity gate still decides registration versus deletion. The migration does not infer a
principal from owner or provenance; legacy private state therefore fails closed to reclamation.
