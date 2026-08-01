# Reject Duplicate Private Image Uploads

## Scope and authority

Issue #1756 requires a second upload of an already registered private image name to preserve the
registered row/object digest invariant, prove that preservation in a regression test, and record
the effect on superseded objects and Systems already using the image. The repository-owner comment
on the issue adds the concurrent same-identity face. The issue explicitly permits rejection or
atomic replacement; ADR-0526 selects rejection.

This change covers the private `images.upload` path, its service reservation, agent-facing wrapper
text, and focused sequential and concurrent tests. It does not add replacement semantics, change
public publication, repair historical corruption, change the schema, or redesign ADR-0525
publication recovery.

## Behavior

After validating the quarantined image and entering the existing PROJECT-locked reservation
transaction, the service queries for a registered private row with the same owner, provider, and
name. Architecture is intentionally absent because the database uniqueness contract for registered
private images is `(owner, provider, name)`.

When the row exists, the service raises `ErrorCategory.CONFLICT` before quota calculation,
pending-row mutation, or any published-prefix object write. The error identifies the existing
image by the caller-supplied name, not by its row UUID. Its only immediate suggested action is
`images.list`: the caller finds the authorized image ID in that result, uses it with
`images.delete`, waits for deletion, and then retries `images.upload`. The MCP wrapper documents
that complete recovery sequence. The quarantined source remains available under its existing
lifecycle; the rejected attempt does not delete it.

Private pending adoption uses that same `(owner, provider, name)` identity. A concurrent first
upload with another architecture adopts the pending row and replaces its architecture, format,
root device, capabilities, provenance, expiry, digest, size, attempt-specific keys, attempt id, and
principal. This complete metadata refresh applies only to private pending attempts. Public pending
retries and defined baselines keep their configuration-owned metadata while refreshing the
attempted object's fields. A defined baseline remains architecture-scoped. The adoptable pending
candidate is selected deterministically by `created_at` then id. Quota accounting locks and
excludes exactly that row, regardless of architecture, before reserving the winner's size; every
other live row remains counted so malformed duplicates or repair races fail closed. Public pending
adoption remains architecture-scoped.

The existing registered row is byte-for-byte unchanged: its id, digest, object key, expiry, and
metadata remain as they were, and the object at its key still matches its digest. No object is
superseded. A System already booted from the old image continues to use its current disk; future
materialization continues to resolve the unchanged registered row.

## Concurrency

The duplicate check and reservation run under the project's existing transaction-scoped advisory
lock. Private finish reacquires PROJECT inside its short registration + audit transaction while its
existing session IMAGE_PUBLISH fence remains held. Thus registration cannot overtake a competing
duplicate precheck before that reservation statement consumes the decision. The ``IMAGE_PUBLISH →
PROJECT`` finish order is acyclic: a reservation holding PROJECT never attempts IMAGE_PUBLISH
before commit, and the finisher takes PROJECT before mutating the catalog row. PROJECT is absent
from validation and every object-store operation.

A sequential duplicate sees the registered row and fails. Two overlapping first uploads may both
begin validation, but their reservation phases serialize. The second adopts the first pending row
before either is registered, including when their architectures differ. ADR-0525's IMAGE_PUBLISH
fence ensures only the current attempt registers. If an earlier attempt already passed
revalidation, it may finish writing only to its own attempt-specific key before registration fails;
existing leaked-object recovery owns that rowless object. An upload whose reservation begins after
registration observes the registered row and fails before writing.

Tests must cover these cases:

1. Register bytes A, attempt the same project/provider/name with bytes B, assert `CONFLICT`, assert
   no new published-prefix PUT, and verify the original row's object hashes to its persisted digest.
2. Force two same-identity first uploads to overlap, assert exactly one registered result and one
   `CONFLICT`, and verify the registered object's bytes hash to the registered digest.
3. Pause the second same-identity upload after its pending-only duplicate precheck but before its
   reservation statement, let the first PUT finish, and prove the first finisher waits for PROJECT.
   Then release the reservation and assert one typed supersession, one registration, two isolated
   attempt keys, and a digest-consistent registered object.
4. Register one architecture, retry the same owner/provider/name under another architecture, and
   assert the architecture-agnostic `CONFLICT` occurs before another published-prefix PUT.
5. Block one first upload's PUT, start the same owner/provider/name under another architecture,
   and assert one registration plus one generic supersession `CONFLICT`, two distinct
   attempt-specific PUT keys, no raw database exception or deletion advice, one quota claim, and a
   digest-consistent winner whose row carries the winning architecture, capabilities, provenance,
   expiry, and registration-audit principal.
6. Seed two cross-architecture pending rows for one private identity, prove deterministic adoption
   of the oldest `(created_at, id)` row, and prove quota excludes only that selected claim while the
   other row's count and bytes remain.

## Failure contract and observability

The conflicts are normal tool failures, not database uniqueness exceptions. A dedicated service
error subtype retains `ErrorCategory.CONFLICT` while letting the MCP handler suggest `images.list`
only for the registered-name case. The failure exposes no row id; the authorized list result is
the source of the UUID required by `images.delete`. Concurrent publication-supersession conflicts
stay generic and must not suggest deleting the winner. The errors carry no secret or cross-project
metadata. The lookup is owner-scoped and parameterized. Quota denial stays
`QUOTA_EXCEEDED`; guest-contract and source-object failures retain their current categories and
precedence because validation still precedes reservation.

No new audit event is introduced. Existing request/tool failure observability reports the typed
error. A registered-name rejection leaves the database and object store unchanged; an in-flight
superseded attempt retains ADR-0525's attempt-key cleanup path.

## Threat model

- **Existing boundary narrowed:** an authenticated project operator supplies `name`, `arch`, and a
  quarantine key to `images.upload`. Existing RBAC remains the caller-side control; the service
  lookup is scoped by the already authorized project and uses SQL parameters.
- **Tenant isolation:** the conflict lookup includes private visibility and owner. An image with the
  same name in another project, or a public image with that name, cannot trigger the conflict or be
  disclosed.
- **Object-write boundary:** untrusted uploaded bytes reach the published prefix only after the
  conflict and quota checks. The regression tests observe the store PUT list and persisted digest,
  making a write-before-reject regression fail in CI.
- **Failure disclosure:** the error may repeat the caller-supplied name and recovery tool names but
  must not reveal another project's existence, row id, object key, digest, or principal.
- **Out of scope:** quarantine retention and historical row/object repair retain their existing
  owners; public/operator publication is not reachable through this MCP path.

## Acceptance checks

- A duplicate registered private name returns `CONFLICT` before a published object write or catalog
  mutation. Its failure identifies the requested name without exposing a UUID, suggests only
  `images.list` immediately, and documents
  `images.list → authorized image ID → images.delete → wait → images.upload` as recovery.
- The registered row remains unchanged and its stored object SHA-256 equals the persisted digest.
- Same-identity concurrent first uploads leave exactly one registered row whose object matches its
  digest. A losing attempt can only write to its isolated attempt-specific key, cannot register it,
  and remains covered by existing leaked-object recovery.
- Different-architecture concurrent first uploads adopt one pending row. The loser returns a
  generic supersession `CONFLICT` without raw database failure or delete advice; both attempts may
  write distinct attempt keys, while the winner's row carries its complete request-owned metadata
  and names bytes matching its persisted digest. Its registration audit names the winning
  principal. Quota records one row and the winner's size.
- With multiple legacy pending rows for one private identity, candidate selection is deterministic
  by `created_at` then id; quota excludes only that selected row and continues to count every other
  live row and byte.
- A forced duplicate precheck-to-reservation interleaving returns a typed supersession rather than
  leaking a database uniqueness exception; PROJECT remains absent during each object PUT.
- Registered private-name rejection intentionally ignores architecture, matching the database
  uniqueness key `(owner, provider, name)`.
- The MCP wrapper docstring exposes the duplicate-name outcome and recovery sequence; other
  `CONFLICT` causes do not advertise deletion.
- Focused service and MCP tests, then `just ci`, pass from the feature worktree.

## Delivery context

- Branch: `feat/reject-duplicate-image-upload-1756`
- Base branch: `main`
- Guardrails: focused `uv run python -m pytest` tests, then `just ci`
- Decision: ADR-0526
