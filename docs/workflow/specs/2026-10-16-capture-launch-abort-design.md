# Private capture spool and verified launch-abort closure

Issue #3225; full-spec, complexity M, fixed denominator 250 changed lines.
Authority: complete WORK:SCOPE on #3225 and the operator's 2026-10-16 approval of
exact design-review-plan SHA256
`d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418`, section #3225.

## Scope and ownership

The capture launcher owns its private runtime root and operation-derived child directories.
The supervisor owns closure of positively verified launcher aborts. Existing publication
recovery owns identity-bound tombstone arbitration. Existing database failure transitions
own credential and job-attempt checks. No ownership moves or public interfaces change.

Host-dump shared spool permissions remain #3220. General handler finalization and
cross-replica guarantees remain excluded by the operator. The exclusion references #2681;
that issue tracks external-build finalization and does not establish broader capture coverage. No schema, dependency, provider, lifecycle-witness,
provisioning, accepted ADR amendment, or live infrastructure changes are included.
ADRs 0558 and 0559 retain their process, publication, and disposal proof requirements.
Optional ADR 0775 is reserved and consumed but unused; no migration is needed.

## Verified causes

A root created under a setgid workspace inherits mode 02700 and fails the current
exact-0700 check. The launcher must clear only that inherited special bit before creating
an attempt. Separately, verified launch-abort cleanup currently acknowledges process exit
without publication closure or spool disposal. The database correctly refuses failure/retry
while those proofs are missing. Two real-Postgres diagnostic cases reproduced the stuck
running job for aborts before spawn and before durable process identity.

## Behavior

1. After creating the runtime root, open it with `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`.
   Inspect that descriptor with `fstat`. Require a directory owned by the effective uid
   whose full mode is exactly 0700 or 02700. Only for 02700, use `fchmod(fd, 0700)`.
   Revalidate owner and exact 0700 before creating the operation directory through that fd.
   Reject other access bits, sticky/setuid bits, symlinks, and foreign ownership without
   repairing them. Attempt, child, result, configuration, and disposal validation stay strict.
2. When launch raises with positive abort evidence, drain mandatory closure while the
   existing capture-job session fence remains held. Acknowledge exit using the supplied
   process/provider evidence, pass the returned exited operation to existing publication
   recovery, remove only its operation-derived private spool, then record spool disposal.
   Preserve the original launch exception for ordinary worker failure handling. Remove
   the old outer abort cleanup path so acknowledgment is not repeated after unlocking.
3. Monitor the lock connection during closure and require worker authority before evidence
   transition, spool removal, and disposal recording. Database transitions retain exact
   credential, attempt, and current-operation checks. No provider gate is released and
   no artifact is published on this path.
4. Publication identity conflict, missing authority, store/database error, or unverified
   spool removal leaves incomplete durable closure recoverable. Ordinary failure cannot
   requeue or dead-letter that operation until existing closure predicates hold. Do not
   fabricate quiescence, discard, absence, or disposal evidence.

## Success

- Real setgid-parent creation and a pre-existing safe 02700 root yield owner-only 0700
  root and attempt directories, with request and configuration I/O and exact disposal.
- Unsafe roots remain rejected; existing strict child/disposal checks remain exercised.
- Verified before-spawn and before-identity aborts close discarded publication and exact
  spool before normal failure returns queued, or failed at exhausted attempt count.
- Closure completes before fence release. Publication/disposal/authority refusal leaves
  the current operation linked and bars retry. Stale credentials/attempts cannot finalize.
- Four live traffic-capture functional cells remain recorded as unproven until a separate
  concrete host/candidate/budget/cleanup plan receives operator authority and runs.

## Failure model

- Actors and deployments: local and remote capture workers using the shared supervisor;
  authenticated tenants request captures through existing handlers.
- Invariants and assets: private packet spools; exact process/provider absence; durable
  publication identity; job-attempt and worker authority before retry or reclamation.
- Accepted failure classes: DB/store failure, identity conflict, unverified removal, or
  authority loss retains recoverable incomplete closure; retry requires the existing proofs.
  A crash during root normalization can leave 02700 for the next validated launch.
- Covered elsewhere: host-dump permissions (#3220); arbitrary handler finalization, worker
  death, and cross-replica guarantees remain excluded without asserting tracked coverage.
  Existing startup recovery handles
  incomplete durable capture closure. Four live cells require separately authorized proof.

Rollback restores prior code; it does not delete durable tombstones or operation rows.

## Threat model

- Boundary inventory: no added boundary. Runtime-root metadata acceptance widens only from
  owner-owned 0700 to owner-owned 02700 followed by exact0700 normalization. Existing process
  evidence, database authority, object-store identity, and spool disposal boundaries remain.
- Actor model: tenants do not control operation ids or worker credentials; local operators
  configure the workspace and are trusted to secure its ancestors. Another local uid may
  create an unsafe path before launch; same-euid and root compromise are outside this repair.
- Control per boundary: nofollow descriptor open, fstat ownership/exact-mode allowlist and
  post-fchmod revalidation govern root admission. Launcher supplies positive absence evidence;
  SQL validates credential/attempt/current link. Existing publication recovery verifies immutable
  operation/object identity and tombstone arbitration. Existing disposal validates exact
  operation-derived children and file modes; only verified removal permits the durable record.
- Explicitly out of scope: administrator/same-euid compromise is outside existing worker trust;
  host-dump threats remain with #3220; broader recovery threats remain excluded without
  asserting tracked coverage.

## Validation

Focused launcher tests exercise creation, existing root normalization, rejected access and
special modes, symlinks, ownership, and descriptor revalidation. Supervisor tests exercise
ordering, cancellation drainage, original exception identity, and refusal paths. Real
Postgres tests use the normal publication coordinator with a store boundary fixture to
prove durable discarded/spool closure, queued retry versus exhausted failure, retained link
on refusal, and existing stale credential/attempt guards. New behavioral tests run red
before code changes and green afterwards. Run affected capture tests, `just lint`, and
whole-tree `just type`; the mandatory managed pre-push hook owns the one final `just ci`.
