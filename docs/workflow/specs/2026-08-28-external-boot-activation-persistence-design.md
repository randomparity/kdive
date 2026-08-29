# External-Boot Activation Persistence Design

## Scope

Issue #2116 implements only the durable persistence slice accepted by ADR-0583, ADR-0584,
and ADR-0585. It adds migration 0121, explicit domain lifecycles, and repository operations
that compare the current activation owner and generation before changing lifecycle truth.

Jobs, MCP tools, provider implementations, authenticated authority allocation, provider-host
journaling, and job-result fencing are excluded. Issue #2125 owns the credential-bound authority
functions and acknowledgement evidence that extend this schema. Issues #2117 and #2118 own
admission, jobs, and reconciliation.

## Goals

- Persist one activation identity per System, Run, and plan.
- Prevent more than one not-fully-cleaned activation per System.
- Persist reservation state, fixed byte debit, immutable store ownership, deadlines, recovery
  point, terminal evidence, and verified cleanup evidence.
- Make every repository mutation an atomic compare-and-set on System, activation, operation owner,
  authority generation, and expected state.
- Reject stale generations without changing activation, reservation, or evidence rows.
- Encode the ADR-0583 lifecycle as an exhaustive domain transition table and prove all legal and
  illegal edges.

## Existing boundaries

`ExternalBootMaterialization` and `RecoveryPoint` in
`src/kdive/providers/ports/external_boot.py` are closed, canonical provider-neutral values. The
persistence layer stores their canonical JSON objects as evidence and revalidates them when rows
are loaded. It does not copy provider paths, credentials, URLs, or libvirt values into core state.

The existing `LockScope.SYSTEM` advisory transaction lock remains the serialization boundary.
Repository write methods require an already-open transaction and acquire that lock before their
compare-and-set statement. The SQL predicate is still load-bearing: a generation can change after
one transaction releases the System lock, so a delayed actor must not become current merely by
acquiring the lock later.

## Domain model

`ExternalBootActivationState` has exactly these members:

`preparing`, `prepared`, `activating`, `active`, `recovering`, `recovered`,
`recovery_conflict`, `recovery_failed`, and `abandoned`.

The legal edges are the ADR-0583 graph:

- `preparing -> prepared | abandoned | recovery_conflict`
- `prepared -> activating | recovering | recovery_conflict`
- `activating -> active | recovering | recovery_conflict`
- `active -> recovering | recovery_conflict`
- `recovering -> recovered | recovery_failed | recovery_conflict`
- `recovery_conflict -> recovering`
- `recovered`, `recovery_failed`, and `abandoned` have no lifecycle exits.

Cleanup is orthogonal to the lifecycle. `recovered` and `abandoned` first persist with
`cleanup_complete=false`; only verified deletion and reservation release can set it true.
`recovery_failed` and `recovery_conflict` ordinarily retain their reservation and evidence, but an
authorized System teardown may set cleanup complete after the System is durably torn down and every
provably activation-owned object is verified absent. Missing or corrupt ownership evidence keeps
the reservation charged and the cleanup flag false.

`ExternalBootReservationState` is `pending -> ready`. A reservation is created with the activation
so no live debit can exist without durable activation ownership. Its owner key, store identity, and
byte count are immutable. Release deletes the live debit row and creates one immutable release
tombstone; absence plus the tombstone is the durable proof that capacity was credited exactly once.

`ExternalBootActivation` is a strict row model containing:

- activation, System, Run, plan, and current operation-owner identities;
- a positive, never-decreasing `authority_generation` used by every mutation predicate;
- lifecycle state and cleanup flag;
- optional activation and recovery deadlines;
- canonical materialization and recovery-point evidence;
- release-request, recovery, terminal, and cleanup evidence; and
- database timestamps.

`ExternalBootReservation` is a strict row model for the separate live debit row.
`ExternalBootReservationRelease` is the immutable release tombstone.
`ExternalBootRecoveryAttempt` records one recovery attempt's identity, acknowledged starting
state, resolution operation, absolute deadline, state, and conflict or terminal evidence. A new
row is created for every `recovery_conflict -> recovering` edge, so later evidence never replaces
the history required to understand an earlier attempt. Evidence fields are bounded JSON objects.
Database constraints cap every JSON evidence document at 65,536 bytes, matching the
provider-neutral canonical-value bound.

## Schema

Migration `0121_external_boot_activations.sql` creates four tables and one supporting unique key
on `runs(id, system_id)`.

`external_boot_activations` has a foreign key to `systems` and a composite foreign key from
`(run_id, system_id)` to `runs(id, system_id)`, a canonical plan digest,
the current operation owner and generation, the lifecycle state, deadlines, JSON evidence fields,
`cleanup_complete`, and timestamps. A partial unique index includes every nonterminal/conflict/
failed state unconditionally and includes `recovered` or `abandoned` only while cleanup is false.
This matches ADR-0583: teardown cleanup may release capacity for a failed/conflicted activation but
does not make that activation a reusable rollback baseline. A unique
`(system_id, run_id, plan_identity)` key makes creation retryable without permitting a rollback
stack. Checks enforce positive generations, digest grammar, closed state values, terminal-only
cleanup, timezone-aware deadlines, document byte bounds, and the row-local parts of the state
matrix below.

`external_boot_reservations` is keyed by `activation_id`, carries the stable store identity,
deterministic owner key, positive reserved byte count, state, readiness evidence, and timestamps. A
unique `(store_identity, owner_key)` key makes retry lookup deterministic. Checks enforce the closed
state set and require `ready_at` plus byte-bounded evidence in `ready`.

`external_boot_reservation_releases` is keyed by `activation_id` and repeats the immutable store,
owner, and byte identities beside bounded release evidence and `released_at`. Creation and deletion
of the matching live reservation happen in one store-locked transaction. A repeated release returns
the existing tombstone only when all immutable identities match; a mismatch is a conflict.

`external_boot_recovery_attempts` is keyed by `(activation_id, attempt_number)` and has a unique
`attempt_id`. It records the authority generation that began the attempt, the optional
administrator resolution operation and acknowledged composite identity, one absolute recovery
deadline, a closed `recovering | conflict | failed | recovered` state, and bounded conflict or
terminal evidence. The activation stores the current `attempt_id`; foreign-key and repository
predicates keep it on the same activation. Attempt numbers increase by one under the System lock
and are never reused. Existing attempt rows retain their deadline and evidence when a later attempt
begins.

The resolution operation, its idempotency identity, and acknowledged composite state are required
on every `recovery_conflict -> recovering` attempt and forbidden on ordinary recovery entries.
An activation also has immutable `pre_recovery_evidence`, written while preparation owns a stable
provider recovery object but before the complete canonical `RecoveryPoint` can be published. A
`preparing -> recovery_conflict` row requires that evidence. It identifies the recorded source,
provider-owned recovery object, and conflicting observation without claiming that source and target
identities were both prepared.

Only the table-owning migration role and `kdive_server` receive direct mutation grants in 0121.
`kdive_server` receives `SELECT`, `INSERT`, and `UPDATE` on activation, live-reservation, and
recovery-attempt rows plus the `DELETE` needed only on live reservations. It receives only `SELECT`
and `INSERT` on immutable release tombstones. Worker and reconciler receive `SELECT` only on all
four tables. Issue #2125 adds security-definer authority allocation and result-fencing functions
and grants those roles `EXECUTE`, never direct table mutation authority.

### State invariant matrix

All states require `cleanup_complete=false` except `recovered` and `abandoned`, which may become
clean after the normal cross-row cleanup predicate succeeds, and `recovery_failed` or
`recovery_conflict`, which may become clean only with teardown cleanup evidence proving the System
is torn down and all provably owned objects are absent. The live-reservation requirements below
mean a matching `ready` reservation row exists and no release tombstone exists.

| State | Required durable evidence | Deadline and reservation requirements |
|---|---|---|
| `preparing` | none initially; persisted materialization may be added once | pending or ready live reservation; no readiness deadline |
| `prepared` | materialization and recovery point | ready live reservation; no readiness deadline |
| `activating` | materialization and recovery point | ready live reservation; activation deadline required and immutable |
| `active` | materialization, recovery point, terminal activation proof | ready live reservation; activation deadline retained |
| `recovering` | materialization, current recovering-attempt row, and either a recovery point or conflict-resolution pre-recovery evidence | ready live reservation; current attempt deadline required and immutable |
| `recovery_conflict` | current conflict-attempt row and its conflict evidence, or immutable pre-recovery evidence when conflict preceded an attempt; materialization when one was published; recovery point when one was prepared | ready live reservation unless authorized teardown cleanup completed; prior attempt deadlines retained |
| `recovery_failed` | materialization, recovery point, and current failed-attempt evidence | ready live reservation; attempt deadline retained |
| `recovered` | materialization, recovery point, and current recovered-attempt evidence | ready live reservation until release; attempt deadline retained |
| `abandoned` | abandonment terminal evidence | pending or ready reservation until release; populated evidence remains immutable |

`prepared`, `activating`, `active`, ordinary `recovering`, `recovery_failed`, and `recovered` therefore
cannot be persisted without the evidence needed to resume them. Conflict may be entered from
`preparing`, so it requires exact pre-recovery ownership and observation evidence but not a recovery
point that may never have been completed. A conflict-resolution recovery may use that evidence only
with its mandatory resolution operation, idempotency identity, and acknowledged composite state.
Row-local checks enforce evidence/deadline presence. Repository transaction predicates enforce the
cross-row reservation rules and evidence immutability.

## Repository interface

`ExternalBootActivationRepository` exposes:

- `create(conn, activation, reservation)` to insert the `preparing` activation and `pending`
  reservation atomically under the System lock;
- `get(conn, activation_id)` and `get_reservation(conn, activation_id)`;
- `mark_reservation_ready(..., recovery_max_bytes)`, which takes the recovery-store advisory lock,
  sums existing `ready` debits for that store, and changes only `pending -> ready` when the new
  total is at most the positive operator-configured cap. `pending` is not a debit. Exact-cap is
  accepted; over-cap returns `capacity_exhausted` without changing the row; retry of an already-ready
  matching reservation returns it without re-debiting or re-evaluating the cap;
- `release_reservation(...)`, which runs under the recovery-store advisory lock, verifies exact
  activation ownership/generation and terminal cleanup eligibility, deletes the live debit, and
  inserts or returns the immutable matching release tombstone;
- `transition(...)`, which validates non-recovery domain edges before SQL and atomically persists
  the new state plus its immutable evidence or activation deadline;
- `record_pre_recovery_evidence(...)`, which can fill the immutable preparation-owned evidence
  once while the activation is `preparing` and refuses a different retry value;
- `begin_recovery_attempt(...)`, which validates an edge into `recovering`, inserts the next
  attempt with its absolute deadline and updates the activation's current attempt in the same
  System-locked transaction. On `recovery_conflict -> recovering`, it requires the resolution
  operation, idempotency identity, acknowledged composite state, and either the full recovery point
  or pre-recovery evidence; on every other source state those resolution fields are forbidden;
- `finish_recovery_attempt(...)`, which atomically changes the current attempt and activation from
  `recovering` to `recovery_conflict`, `recovery_failed`, or `recovered`, retaining bounded evidence;
  and
- `mark_cleanup_complete(...)`, which requires `recovered` or `abandoned` for ordinary cleanup, or
  `recovery_failed`/`recovery_conflict` plus teardown evidence for teardown cleanup, no live
  reservation, a matching release tombstone, cleanup evidence, and the exact current
  owner/generation.

Every mutating method takes the exact `system_id`, `activation_id`, `operation_owner_id`, and
`authority_generation`. The query filters on all four plus its expected state. It returns a tagged
result: `applied` with the current row, `superseded` when no row matches the complete predicate,
or `not_found` only when the activation identifier does not exist. `superseded` deliberately does
not reveal which authority component mismatched.

Repository methods never allocate or advance authority generations. That authenticated operation
belongs to issue #2125. This slice persists the current values and proves a stale value affects zero
rows, which is the contract later security-definer functions consume.

## Deadline and evidence rules

The repository accepts only absolute timezone-aware UTC datetimes. The first transition to
`activating` sets `activation_readiness_deadline`; later writes may not change it. Every edge into
`recovering` creates one attempt with its own immutable `recovery_readiness_deadline`.
`recovery_conflict -> recovering` therefore starts a new attempt without rewriting the prior
deadline. Ordinary retry resumes the same attempt and cannot extend it.

Activation-wide materialization, recovery-point, activation, abandonment, and cleanup evidence use
explicit immutable slots. Recovery conflict and terminal evidence is attempt-scoped instead: a
retry may repeat byte-identical evidence on its current attempt, but it may not replace it with a
different value. A later administrator resolution creates a new attempt and can therefore retain a
new acknowledged identity, resolution operation, deadline, and subsequent conflict observation
without erasing earlier evidence.

Cleanup ordering is enforced across the three rows: store-locked reservation deletion plus release
tombstone creation is persisted before `cleanup_complete`. `mark_cleanup_complete` runs under the
System lock, verifies reservation absence and the matching tombstone, records exact cleanup
evidence, and sets the activation flag in one transaction. An interrupted cleanup therefore remains
charged or leaves a terminal activation with `cleanup_complete=false`; neither state admits a
replacement activation.

For `recovery_failed` and `recovery_conflict`, `release_reservation` additionally requires
teardown evidence that identifies the torn-down System and proves every individually owned object
absent. If ownership is missing, corrupt, or quarantined, it returns a retained-capacity result and
changes neither live debit nor activation. Successful teardown cleanup preserves all conflict/
failure and attempt evidence even after capacity is released.

## Error handling

Invalid domain edges raise `IllegalTransition` before SQL. Invalid deadline or evidence combinations
raise `ValueError` before SQL. A missing activation is distinct from a stale compare-and-set, but
all authority mismatches share the `superseded` outcome. Database constraints remain the final guard
against a caller outside this repository.

The repository does not retry serialization, capacity, or lock failures. Its later service/job
callers retain responsibility for mapping operational database failures into the existing error
taxonomy.

## Threat model

### Boundary inventory

- Added: validated domain records cross from server-owned core logic into Postgres.
- Added: later worker and reconciler callers will present operation-owner and generation claims to
  repository compare-and-set operations.
- Not widened: provider references remain the existing bounded `OpaqueProviderRef` values inside
  validated canonical evidence; this slice adds no network, command, path, URL, or secret input.

### Actors and trust

Authenticated tenants can cause later server admission but cannot write these tables directly.
Workers and reconcilers are trusted runtime roles whose process credentials may become stale after
takeover. They have no direct mutation grants on these tables. Postgres and the server-side
System-lock protocol are trusted. A database administrator is outside this boundary.

### Controls

- Pydantic closed models and PostgreSQL checks validate states, digests, timestamps, sizes, and
  evidence presence.
- The per-System advisory transaction lock serializes supported writers.
- The recovery-store advisory transaction lock makes capacity summation plus ready-state debit and
  deletion plus release-tombstone creation atomic across Systems.
- The complete SQL compare-and-set predicate denies stale operation owners and generations without
  partial writes.
- Partial uniqueness denies concurrent unfinished activations even if a caller omits the read-side
  admission check.
- Runtime grants give worker/reconciler roles read-only table access; later writes are possible only
  through issue #2125's credential-bound security-definer functions.
- Evidence documents are byte-bounded and never surfaced by this persistence-only change.

### Out of scope

Credential authentication, generation allocation, provider acknowledgement matching, audit trails,
and job-result fencing are intentionally deferred to issue #2125, which is blocked on this schema.
Provider-host positive quiescence and journal integrity belong to issues #2126 and #2127. This slice
does not claim that a bare owner UUID and generation authenticate an actor; it only makes current
ownership durable and stale compare-and-set writes impossible through this repository.

## Verification

- Exhaustive table-driven tests prove every legal and illegal activation and reservation edge.
- Property tests generate all same-enum state pairs and prove the transition table is complete.
- Migration tests prove every positive/negative state-matrix constraint, the composite Run/System
  foreign key, JSON bounds, partial uniqueness, stable reservation ownership, recovery-attempt
  identity, immutable release tombstones, and runtime-role grants.
- Repository tests prove round trips, every state-matrix prerequisite, immutable deadlines/evidence,
  concurrent two-System capacity admission, exact-cap and over-cap behavior, cleanup ordering,
  idempotent reservation release, successful and retained-capacity teardown branches, two distinct
  conflict-to-recovery attempts, a preparing conflict resolved without a fabricated recovery point,
  rejection of missing resolution identity/operation, wrong-System/owner/generation rejection, and
  that a stale-generation compare-and-set changes no activation, reservation, recovery attempt,
  release tombstone, or evidence value.
- `just lint`, `just type`, focused domain/database tests, and `just ci` are the required guardrails.
