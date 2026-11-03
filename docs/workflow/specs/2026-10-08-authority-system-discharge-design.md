# Receipt-bound exhausted authority System discharge

## Problem

Issue #3143 requires an expired final provision attempt without a terminal receipt
to reach a terminal or recoverable state, and allocation release to be admitted or
name the exact supported clearing action. At base `5af33e124`, real Postgres tests
with restricted worker/authority roles reproduce acknowledged attempt 3/3 remaining
running with no claim or terminal-receipt repair. A second DB diagnostic consumes a
preactivation-absence receipt successfully but release still raises the preactivation
fence. The actual ADMIN teardown handler also admits the exhausted seeded System
and queues the authority teardown under the restricted server role. These are
observed DB/service results, not native POWER reproduction.

## Scope and decision

[ADR-0754](../../adr/0754-receipt-bound-authority-system-discharge.md) reuses
explicit `systems.teardown` as the supported discharge. Implement receipt-bound
cancellation of the exhausted predecessor and a release-only proven-absence exception.
The operator approved this concrete discharge policy on 2026-10-12.
The design does not automatically retry provisioning or automatically destroy a System.

The frozen campaign scope permits receipt-safe job recovery, migration and DB tests.
Generic authority dead-lettering, DWARF #3130/#3131, provider conflict #3144 and durable
finalization #2681 remain excluded. Native POWER obligations remain open. No provider
intent-matching code, retry budget, external-boot consumption table or claim predicate
changes are made. Current ownership stays with authority receipt finalization;
this is a clean extension of its existing reconciler consumption path.

## Components and data flow

1. Existing ADMIN-gated `systems.teardown` admits preactivation ownership and queues
   the existing authority teardown. Its generation takeover and authority service
   establish quiescence and genuine absence; this design does not replace that proof.
2. Existing worker finalization or cleanup-gated reconciler finalization consumes
   the exact `preactivation-absent` receipt and sets ownership/System torn down.
3. Migration 0171 adds a restricted boolean proof reader for server/reconciler.
   Require same ownership/System, no first activation, current terminal teardown
   attempt, consumed preactivation-absence receipt, and matching terminal sequence/
   digest with the durable ownership head. No caller-supplied receipt is accepted.
4. Only `ALLOCATION_RELEASE` uses that boolean to bypass the preactivation fence;
   subsequent external-boot admission checks remain. Allocation release still checks
   each historical System: one absent System cannot excuse another restricted one.
   Other mutation operations keep
   the existing fence. Existing denial next actions retain `systems.teardown`.
5. A bounded reconciler-only SQL repair uses the same proof after normal terminal
   authority repairs. It finds matching `authority_system_v1` provision jobs with
   expired running leases and exhausted budgets. Bind allocation, resource, provider,
   authority, root and profile identities to ownership; operation must be provision.
   Require `jobs.created_at <= receipt_at`: this is the enqueue/recycle cutoff,
   not a universal retry-budget epoch. `queue.enqueue_with_status` resets created_at
   on recycle; 0149 retained-receipt extensions do not, and are controlled separately
   by the ownership/attempt/receipt fences. Exclude active allocating/
   current attempts and unconsumed terminal attempts. A predecessor attempt, if present,
   must belong to this exact job/System and be superseded; no attempt is also valid
   when the worker died before allocating it. Never consume or forge its receipt.
6. Under the authority System advisory lock and ownership/receipt/job row locks,
   revalidate the complete predicate and cancel the old job, clear stale worker/lease/
   error fields, and retain the successor receipt digest in `result_ref`. Use the
   existing canceled state, not fabricated success or a new failure category.
   The scan applies complete eligibility before LIMIT 100, then revalidates under
   locks; an earlier ineligible prefix cannot starve eligible residue. Locked
   candidates defer.
   Existing already-consumed absence rows are included, so pre-upgrade residue repairs.

## Persistence, concurrency and errors

Migration 0171 is additive; accepted migrations remain immutable. The proof reader
is callable only by server/reconciler; cancellation only by reconciler. Both enforce
session-role membership with empty search paths and revoked PUBLIC execution.
No runtime role gains raw table writes or receipt creation authority.

Lock ordering follows authority finalizers: System authority lock, ownership,
receipt attempt, then job. Re-read after acquiring locks. Claim/count eligibility
and attempt limits remain unchanged; test their parity before and after discharge.
An active lease, enqueue/recycle timestamp newer than the receipt, foreign marker,
active attempt, unconsumed terminal
receipt, activated owner, conflicting head or incomplete cleanup must prevent repair.
Failure rolls back that transaction, leaving evidence for a later pass. Repeating a
successful pass is a no-op. Concurrent receipt completion and operator teardown must
produce either retained work or one receipt-backed cancellation, never premature release.

Migration rollback is a forward corrective migration, not editing 0149 or erasing
receipts. Deployment applies schema before code invoking the new functions. Existing
workers continue receipt finalization; the reconciler supplies predecessor repair.
No automatic policy activates until operator-selected teardown has proved absence.

## Success

- The reproduced final-attempt row is unchanged before the supported teardown proof.
  After successful teardown consumption, its job becomes canceled through the bounded
  repair and allocation release succeeds through the ordinary service path.
- The same repair clears equivalent already-discharged residue present before upgrade.
- Worker and reconciler receipt-consumption paths both reach that result; stale or
  forged tuples, live workers, unconsumed receipts and foreign identities remain fenced.
- Repeated and concurrent repair does not duplicate state changes or expand attempt
  budgets. Ordinary jobs and external-boot authority jobs retain their existing behavior.
- Operator guidance names exact teardown/poll/release actions and required existing
  authority health. A failed provider proof is retained and reported, never counted as pass.

## Failure model

- Actors and deployments: authenticated project ADMIN selecting teardown, contributor
  releasing allocation, registered worker, reconciler and provider-authority services;
  installed local/remote authority deployments and native x86_64 DB/worker proof.
- Invariants and assets: authority-only absence evidence, immutable receipt identity,
  capacity ownership, tenant isolation, lifecycle ordering and bounded retries.
- Accepted failure classes: unavailable authority retains the operation until its
  documented service is restored; provider conflict retains quarantine under #3144.
  Neither is an excuse to claim the required end-to-end proof passed.
- Covered elsewhere: native POWER #2818; DWARF #3130/#3131; provider intent conflict
  #3144; generic durable finalization #2681. No generic authority dead-letter path.

## Threat model

- Boundary inventory: new read-only proof function and reconciler cancellation function
  cross private authority tables; allocation-release admission gains one exact exception.
  Existing tenant-to-ADMIN teardown and worker-to-authority receipt boundaries do not widen.
- Actors: a tenant may request release or teardown but cannot supply trusted receipts;
  worker credentials may only execute existing worker functions; authority and DB owner
  remain trusted for durable receipt production and schema maintenance.
- Controls: existing project/RBAC admission; function membership checks; exact stored
  identity joins; immutable terminal bytes; lease/state/attempt fences; bounded batch;
  transactional lock ordering. Failure reveals only existing System/job identifiers and
  supported actions, never credentials, private paths or raw receipt bytes.
- Out of scope: compromised database owner or provider authority, unsupported manual
  state surgery, excluded provider intent repair, native POWER qualification.

## Validation

Real Postgres tests must exercise genuine authority journal/receipt publication and
restricted production roles, not merely seed a purported absence boolean. Include
registered-worker provision loss, supported teardown dispatch, provider observation/
receipt, both finalization paths, reconciler cancellation, and actual allocation release.
Use an existing authority test provider only at the external provider boundary; state
which provider effects are real and request a concrete native host if installed-authority
proof requires it. Local DB success alone is not native installed-authority proof.

Cover all predicate refusals, count/claim parity, role permissions, replay, migration
application to old residue, more than 100 earlier ineligible rows before an eligible
row, and concurrent attempts. Controlled proof-removal mutations
must turn acceptance tests red. Focused tests, lint, whole-tree types, records/docs and
hooks precede commits; installed mandatory pre-push owns full `just ci`, remote CI separate.
