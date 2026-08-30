# Provider-host external-boot authority and journal implementation plan

**Goal:** Implement issue #2126's provider-host authority, exact journal recovery, database head
checkpoint, and shared bounded mutation-adapter contract.

**Architecture:** A provider-neutral authority service validates migration 0122 bindings, serializes
one lane per System, journals and anchors every mutation phase, and delegates only typed commit
points to a provider adapter. Migration 0123 owns an independent monotonic journal head; concrete
local and remote adapters and composition are owned by #2140.

**Tech stack:** Python 3.14, Pydantic, asyncio, psycopg 3, PostgreSQL, pytest, Hypothesis, libvirt.

## Global Constraints

- Python 3.14; x86_64 and ppc64le targets; no new dependency.
- Protocol `external-boot-authority-v1`; closed serialized input capped at 1 MiB.
- Identifiers are nonblank and at most 255 UTF-8 bytes, provider identities at most 1,024 UTF-8
  bytes, recovery-object lists at most 1,024 entries, generations/sequences positive signed 64-bit,
  and digests `sha256:` plus 64 lowercase hexadecimal digits.
- Requests and diagnostics contain identities and bounded observations, never provider definitions,
  paths, commands, credentials, secrets, or raw provider output.
- Postgres remains lifecycle truth. The journal records provider admission and observation only.
- `watermark-installed`, recoverable `takeover-superseded`, and `takeover-acknowledged` are
  anchored only for the newest exact allocating binding. Already-started lower operations may
  append bounded completion evidence after the watermark; new mutation phases require the promoted
  current binding.
- `admitted` and `mutation-started` are locally fsynced and database-anchored before provider
  access. Every provider commit gets a fresh binding/generation check.
- Takeover acknowledgement waits for positive resolution of every older admitted call. Timeout,
  cancellation, disconnect, or presumed death is not quiescence.
- Local recovery must equal the complete trusted database head. Shorter, longer, divergent,
  reordered, corrupt, duplicate, or foreign history fails closed; no automatic trimming or head
  rollback.
- Recovery-object ownership remains `(System, activation, recovery reference)` across takeover.
- Scheduling, reconciliation, lifecycle transitions, deployment provisioning/ACL rollout, and
  non-external provider behavior are unchanged.
- Use `just` recipes; run gates bare. Commits are Conventional Commits, imperative, and at most 72
  characters.

## Task 1: Define the closed provider-neutral protocol and journal codec

**Files:** Create `src/kdive/providers/external_boot_authority/__init__.py`, `protocol.py`, and
`journal.py`; create `tests/providers/external_boot_authority/test_protocol.py` and
`test_journal.py`.

**Interfaces:** Define `AuthorityTakeoverRequestV1`, `AuthorityMutationRequestV1`,
`AuthorityAcknowledgementV1`, `AuthorityObservationV1`,
`RecoveryObjectBindingV1`, `JournalRecordV1`, `JournalPhase`, `canonical_record_bytes(record) ->
bytes`, `record_digest(record) -> str`, and `FileAuthorityJournal.append(record) -> None` /
`load() -> tuple[JournalRecordV1, ...]`. Later tasks consume only these public values.

1. Write parameterized tests for every valid request field and for blank, over-byte-limit,
   over-cardinality, uppercase/bad digest, zero/overflow integer, extra-field, forbidden payload,
   duplicate recovery reference, reordered ownership, and over-1-MiB cases. Run
   `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q`; expect
   collection to fail because the package does not exist.
2. Implement closed Pydantic values, byte validators, canonical recovery ordering, and the
   serialized-size guard. Make journal records a discriminated union: takeover phases forbid
   source/target/recovery fields, while mutation phases require them. Give
   `AuthorityAcknowledgementV1` separate journal sequence, journal digest, and
   positive-quiescence-digest fields. Rerun the command; expect all protocol tests to pass.
3. Write journal tests for genesis, previous-digest chaining, takeover and mutation phase ordering,
   exclusive mode-0600 creation without symlink following, parent-directory fsync before the first
   checkpoint, and rejection of an existing path that is not a regular service-owned file or has
   group/other write bits. Cover canonical newline-delimited encoding, partial final records,
   duplicate/reordered sequences,
   foreign lane identity, ownership mutation, and corrupt digests. Run the journal test file;
   expect failures for missing codec and journal.
4. Implement the canonical JSON codec and journal using descriptor-based exclusive/no-follow open,
   restrictive mode checks, `flush`, file `os.fsync`, and parent-directory `os.fsync` on creation.
   Reject rather than repair invalid bytes. Rerun both files; expect all tests to pass.
5. Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit the explicit Task 1
   paths as `feat(providers): define authority journal protocol`.

**Acceptance:** All malformed values fail before I/O; valid records have deterministic bytes and
digest chains; loading never normalizes or silently drops journal bytes.

## Task 2: Add migration 0123 and the journal-head repository

**Files:** Create `src/kdive/db/schema/0123_external_boot_authority_journal.sql`,
`src/kdive/db/external_boot_authority_journal.py`, and
`tests/db/test_external_boot_authority_journal_migration.py`; update only migration inventory
expectations that enumerate every schema file.

**Interfaces:** Define `AuthorityBinding` and `JournalHead` repository values,
`resolve_allocating_authority_binding(conn, *, peer_incarnation_id, authority_id, generation) ->
AuthorityBinding | None`, `resolve_current_authority_binding(conn, *, peer_incarnation_id,
authority_id, generation, acknowledgement_sequence, acknowledgement_digest) -> AuthorityBinding |
None`, `read_journal_head(conn, *, binding) -> JournalHead | None`, and
`advance_journal_head(conn, *, binding, expected_sequence,
expected_digest, record) -> Literal["advanced", "superseded", "conflict"]`. Task 3 consumes this
repository protocol; it does not issue SQL directly.

`JournalHead` includes bounded nullable `PendingTakeover` and `SuspendedOperation` values. The
former retains takeover authority/generation/operation/attempt/request digest and watermark head;
the latter retains the one serialized lower operation's authority/generation/System/activation/
Run/plan/provider/authority instance, operation identity/attempt/purpose/exact adapter operation or
commit point/request digest, prior phase, and source/target/ownership digests.

1. Write migration tests for the table constraints, genesis insertion, exact 0122 binding lookup,
   binding-scoped trusted-head read, authority-role execute grants, denied worker/reconciler/core
   reads and writes, and no lifecycle permissions.
   Run `just test-verbose tests/db/test_external_boot_authority_journal_migration.py`; expect a
   missing-migration failure.
2. Add concurrent compare-and-set tests: one successor advances, stale expected heads change zero
   rows, duplicate identical replay is idempotently classified, different duplicate facts conflict,
   phase regression and operation switch conflict, generation/binding mismatches supersede, and
   signed-64-bit overflow fails without writes. Include foreign System/activation/Run/attempt,
   provider/instance/purpose/operation/digest/peer cases.
3. Add a complete binding-state/phase matrix: only the newest exact allocating authority may anchor
   takeover records; only the matching promoted current authority may admit or start mutations.
   After a watermark, only the authenticated pending successor may append inherited
   returned/observed/terminal records for an already-started lower operation, or terminal
   `never-began` directly from its anchored admitted record, without impersonating the lower
   binding. Cross-state, cross-phase, stale, and unrelated
   attempts change zero rows. Race allocations between watermark and acknowledgement and prove the
   winner anchors `takeover-superseded`, inherits unresolved operations, and makes progress. Assert
   the acknowledgement exposes its anchored sequence/digest and a separately constructed canonical
   positive-quiescence digest for migration 0122.
4. Add continuation-state tests. Watermark CAS atomically snapshots the exact nonterminal head into
   one `SuspendedOperation` and installs `PendingTakeover`; completion must match every retained
   immutable field and phase; terminal completion clears the suspended state and returns only to
   that takeover; exact newer supersession replaces only the pending takeover. Fabricated lower
   operations, wrong admitted/started phase, wrong attempt/request/ownership digest, unrelated
   takeover, partial-null groups, and over-bound values change zero rows. Exact
   `takeover-acknowledged` atomically clears its matching `PendingTakeover`. Prove the only successor
   order is `G watermark-installed` → `H takeover-superseded` → `H watermark-installed`; skipped,
   reversed, duplicated, or foreign transitions change zero rows.
5. Implement migration 0123 with the one-row-per-lane head, bounded continuation columns,
   constraints, security-definer resolve
   and compare-and-set functions, pinned `search_path`, explicit revokes, and exact execute grants.
   Acquire the System advisory lock before the head row and re-resolve the 0122 binding under lock.
6. Implement the typed psycopg repository wrapper and prove binding-scoped head reads plus all three
   SQL outcomes without swallowing database failures. Rerun the migration file; expect all tests to
   pass.
7. Update the existing migration-count/name assertions, regenerate nothing by hand, then run
   `just test-verbose tests/db/test_migrate.py tests/db/test_external_boot_authority_migration.py
   tests/db/test_external_boot_authority_journal_migration.py`; expect all tests to pass.
8. Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 2 paths
   as `feat(db): anchor authority journal heads`.

**Acceptance:** Only the authority role can resolve and advance heads; every mismatch is zero-write;
the trusted head is exact and monotonic and cannot be deleted or moved backward by runtime roles.

## Task 3: Implement serialized authority lanes and restart recovery

**Files:** Create `src/kdive/providers/external_boot_authority/service.py` and
`tests/providers/external_boot_authority/test_service.py`; extend `protocol.py` only for a service
result proven necessary by these tests.

**Interfaces:** Define `AuthenticatedPeer(incarnation_id: UUID)`, `AuthorityMutationAdapter` with
`observe(request) -> Awaitable[AuthorityObservationV1]` and `commit(request, commit_point) ->
Awaitable[AuthorityObservationV1]`. `ExternalBootAuthorityService.acknowledge_takeover(peer,
request) -> AuthorityAcknowledgementV1` installs the watermark and quiesces older calls without
provider mutation. `execute_mutation(peer, request) -> AuthorityObservationV1` requires the exact
current 0122 binding and recorded acknowledgement. It consumes Task 1's codec/journal and Task 2's
repository.

1. Write a fake repository, journal, and controllable adapter. Test unauthenticated/inactive peer,
   stale/cross-binding request, malformed ownership, and failed readiness all produce no journal or
   provider access. The fake's binding-scoped `read_journal_head` drives startup equality and
   mismatch cases. Run the service file; expect import failures.
2. Prove takeover accepts only the newest exact `allocating` binding, performs no provider mutation,
   anchors `watermark-installed` before quiescing older calls, and returns the final
   `takeover-acknowledged` sequence/digest plus the canonical positive-quiescence digest. Prove an
   intervening newer allocation follows exactly `G watermark-installed` →
   `H takeover-superseded` → `H watermark-installed`, inherits unresolved work, and cannot strand
   the lane; reject reversed or skipped phases. Prove mutation is denied until core promotes the
   binding to `current` with the exact acknowledgement sequence and digest.
3. Add concurrency tests that pause an old operation before and after every commit point, admit a
   successor, and prove acknowledgement remains pending until the old call is positively observed.
   Cover the admitted-before-start race with a provider-access-free terminal `never-began`, plus
   cancellation and client disconnect while a started lane task continues.
4. Add exact phase-order tests proving `admitted` and `mutation-started` are fsynced and anchored
   before the first adapter call, and later observations are anchored before acknowledgement or a
   later commit. Prove the only nonterminal operation switches are watermark to exact inherited
   completion, inherited terminal back to its takeover, and watermark to the exact newer allocating
   takeover's supersession record. Inject local-append, fsync, database-CAS, provider-return, and
   observation failures.
5. Add restart matrices for every journal phase, including both takeover records, and reject
   empty-with-head, valid-prefix truncation, extra suffix, corruption, reorder, duplicate sequence,
   foreign lane, divergent phase/operation, and changed stable ownership. Include a trusted
   `mutation-started` head with unresolved provider work and prove takeover remains withheld. Inject
   crashes around exclusive journal creation, parent-directory fsync, and initial CAS; a committed
   head must never survive a missing directory entry.
6. Implement lane state, startup recovery, phase append+anchor, pre-commit binding rechecks,
   cancellation shielding, positive observation classification, and bounded structured errors.
   Add bounded structured logs and metrics for rejection category, recovery failure, unresolved
   older calls, and checkpoint latency. Labels are provider kind and authority instance only, never
   tenant-controlled values. Do not add lease/deadline promotion or automatic journal repair.
7. Rerun `just test-verbose tests/providers/external_boot_authority`; expect all tests to pass.
   Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 3 paths
   as `feat(providers): serialize authority mutations`.

**Acceptance:** A System has one ordered mutation lane; an older unresolved call blocks takeover
through cancellation and restart; no provider commit occurs without a current exact binding; every
returned acknowledgement is backed by the exact fsynced and anchored terminal evidence.

## Moved task: Integrate local and remote libvirt adapters

Concrete local-libvirt and remote-libvirt `AuthorityMutationAdapter` implementations, composition,
configured-coordinate proofs, bounded provider-error mapping, and advertisement gating moved to
#2140. They depend on the real external-boot primitives owned by #2108, #2110, and #2120 and are not
an executable task in this plan.

## Task 4: Prove the complete core authority boundary

**Files:** Create `tests/adversarial/test_external_boot_authority_journal.py`; update the design
spec only if implementation revealed a factual correction, never to weaken a failed proof.

**Interfaces:** Consume the public protocol, service, repository, and Task 3's controllable shared
adapter contract exactly as implemented above; define no production API or composition binding.

1. Write adversarial races covering two generations, stale retries with successful idempotency
   keys, later Run versus earlier completed Run, lost response at every commit point, restart with
   an unresolved call, conflict/source/target classification, release/teardown, and stable recovery
   ownership. Run the file with a controlled fault in the generation or head comparison and confirm
   at least one test fails; revert the fault.
2. Prove `readiness` remains false for journal/head disagreement and unresolved recovery and becomes
   true only for exact recovered continuity. Prove no production assembly advertises v1 in this
   issue; authenticated hosting belongs to #2127 and concrete provider composition belongs to
   #2140.
3. Run all focused provider and migration commands from the spec; expect all tests to pass. Run
   `just lint` and `just type`; expect exit 0. Assert recovery failures, unresolved-call gauges,
   rejection counters, and checkpoint latency are bounded and carry no tenant-controlled labels or
   provider output.
4. Run `just ci` bare; expect exit 0. Record environment-gated live tiers as not run unless their
   prerequisites are present; do not treat a skip as a live proof.
5. Re-read `git diff main...HEAD`, verify wrapper docs are unaffected, run `git diff --check`, and
   commit the explicit adversarial test path and any factual spec correction as
   `test(providers): prove authority takeover fencing`.

**Acceptance:** The branch proves all narrowed #2126 completion criteria and threat-model controls
through the shared adapter contract. No concrete provider integration, assembly advertisement,
excluded subsystem, or deployment promise is introduced.

## Rollback and cleanup

Migration 0123 is additive. Before any deployment advertises v1, rollback may stop the authority
service and leave its head rows and journal files retained for audit; it must not delete or rewind
either. After advertisement, rollback to direct mutation is forbidden because it bypasses the
accepted fence. Failed and temporary test journals use pytest-managed temporary directories and
are cleaned by fixtures. Production journal repair is only exact-byte restoration by an audited
operator and is outside the runtime API.
