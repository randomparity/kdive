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
name. Architecture is intentionally absent from this lookup because the database uniqueness
contract for registered private images is `(owner, provider, name)`.

When the row exists, the service raises `ErrorCategory.CONFLICT` before quota calculation,
pending-row mutation, or any published-prefix object write. The error identifies the existing
image and tells the caller to use `images.delete`, wait for deletion, and then retry
`images.upload`. The MCP wrapper documents the conflict and recovery sequence. The quarantined
source remains available under its existing lifecycle; the rejected attempt does not delete it.

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
begin validation, but their reservation phases serialize. The second can adopt the first pending
row before either is registered. ADR-0525's IMAGE_PUBLISH fence ensures only the current attempt
registers. If an earlier attempt already passed revalidation, it may finish writing only to its own
attempt-specific key before registration fails; existing leaked-object recovery owns that rowless
object. An upload whose reservation begins after registration observes the registered row and
fails before writing.

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

## Failure contract and observability

The conflict is a normal typed tool failure, not a database uniqueness exception. A dedicated
service error subtype retains `ErrorCategory.CONFLICT` while letting the MCP handler attach
delete-then-upload actions only to the registered-name case. A concurrent publication-supersession
`CONFLICT` must not suggest deleting the winner. The error carries no secret or cross-project
metadata. The lookup is owner-scoped and parameterized. Quota denial stays `QUOTA_EXCEEDED`;
guest-contract and source-object failures retain their current categories and precedence because
validation still precedes reservation.

No new audit event is introduced. Existing request/tool failure observability reports the typed
error, while the database and object store remain unchanged by the rejected publish attempt.

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
  mutation and names `images.delete` followed by `images.upload` as recovery.
- The registered row remains unchanged and its stored object SHA-256 equals the persisted digest.
- Same-identity concurrent first uploads leave exactly one registered row whose object matches its
  digest. A losing attempt can only write to its isolated attempt-specific key, cannot register it,
  and remains covered by existing leaked-object recovery.
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
