# Provider-host external-boot authority and journal implementation plan

**Goal:** Implement issue #2126's provider-host authority, exact journal recovery, database head
checkpoint, and equivalent local/remote libvirt adapters.

**Architecture:** A provider-neutral authority service validates migration 0122 bindings, serializes
one lane per System, journals and anchors every mutation phase, and delegates only typed commit
points to a provider adapter. Migration 0123 owns an independent monotonic journal head; local and
remote adapters share the same bounded port and contract tests.

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
   serialized-size guard. Rerun the command; expect all protocol tests to pass.
3. Write journal tests for genesis, previous-digest chaining, phase ordering, append+fsync,
   canonical newline-delimited encoding, partial final records, duplicate/reordered sequences,
   foreign lane identity, ownership mutation, and corrupt digests. Run the journal test file;
   expect failures for missing codec and journal.
4. Implement the canonical JSON codec and journal using `Path.open`, `flush`, and `os.fsync`.
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
None`, and `advance_journal_head(conn, *, binding, expected_sequence,
expected_digest, record) -> Literal["advanced", "superseded", "conflict"]`. Task 3 consumes this
repository protocol; it does not issue SQL directly.

1. Write migration tests for the table constraints, genesis insertion, exact 0122 binding lookup,
   authority-role grants, denied worker/reconciler/core table writes, and no lifecycle permissions.
   Run `just test-verbose tests/db/test_external_boot_authority_journal_migration.py`; expect a
   missing-migration failure.
2. Add concurrent compare-and-set tests: one successor advances, stale expected heads change zero
   rows, duplicate identical replay is idempotently classified, different duplicate facts conflict,
   phase regression and operation switch conflict, generation/binding mismatches supersede, and
   signed-64-bit overflow fails without writes. Include foreign System/activation/Run/attempt,
   provider/instance/purpose/operation/digest/peer cases.
3. Implement migration 0123 with the one-row-per-lane head, constraints, security-definer resolve
   and compare-and-set functions, pinned `search_path`, explicit revokes, and exact execute grants.
   Acquire the System advisory lock before the head row and re-resolve the 0122 binding under lock.
4. Implement the typed psycopg repository wrapper and prove it maps all three SQL outcomes without
   swallowing database failures. Rerun the migration file; expect all tests to pass.
5. Update the existing migration-count/name assertions, regenerate nothing by hand, then run
   `just test-verbose tests/db/test_migrate.py tests/db/test_external_boot_authority_migration.py
   tests/db/test_external_boot_authority_journal_migration.py`; expect all tests to pass.
6. Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 2 paths
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
   provider access. Run the service file; expect import failures.
2. Prove takeover accepts only the newest exact `allocating` binding, performs no provider mutation,
   and returns anchored acknowledgement evidence. Prove mutation is denied until core promotes the
   binding to `current` with the exact acknowledgement sequence and digest.
3. Add concurrency tests that pause an old operation before and after every commit point, admit a
   successor, and prove acknowledgement remains pending until the old call is positively observed.
   Cover cancellation and client disconnect while the lane task continues.
4. Add exact phase-order tests proving `admitted` and `mutation-started` are fsynced and anchored
   before the first adapter call, and later observations are anchored before acknowledgement or a
   later commit. Inject local-append, fsync, database-CAS, provider-return, and observation failures.
5. Add restart matrices for every journal phase and reject empty-with-head, valid-prefix truncation,
   extra suffix, corruption, reorder, duplicate sequence, foreign lane, divergent phase/operation,
   and changed stable ownership. Include a trusted `mutation-started` head with unresolved provider
   work and prove takeover remains withheld.
6. Implement lane state, startup recovery, phase append+anchor, pre-commit binding rechecks,
   cancellation shielding, positive observation classification, and bounded structured errors.
   Do not add lease/deadline promotion or automatic journal repair.
7. Rerun `just test-verbose tests/providers/external_boot_authority`; expect all tests to pass.
   Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 3 paths
   as `feat(providers): serialize authority mutations`.

**Acceptance:** A System has one ordered mutation lane; an older unresolved call blocks takeover
through cancellation and restart; no provider commit occurs without a current exact binding; every
returned acknowledgement is backed by the exact fsynced and anchored terminal evidence.

## Task 4: Add bounded local and remote libvirt adapters

**Files:** Create `src/kdive/providers/local_libvirt/external_boot_authority.py` and
`src/kdive/providers/remote_libvirt/external_boot_authority.py`; update the corresponding
`composition.py` files only to construct the bounded adapter; create matching
`tests/providers/local_libvirt/test_external_boot_authority.py` and
`tests/providers/remote_libvirt/test_external_boot_authority.py`.

**Interfaces:** Each module implements Task 3's `AuthorityMutationAdapter`. Local construction takes
the configured libvirt URI; remote construction takes the already resource-bound
`RemoteLibvirtConfig` and existing connection factory. Neither accepts transport coordinates from
`AuthorityMutationRequestV1`.

1. Write one shared behavioral test matrix and invoke it for local and remote adapters. It covers
   exact source, target, mixed/unreadable/conflict observations; every external-boot commit point;
   stable recovery ownership; bounded exception mapping; and rejection of unsupported commit
   points. Run both files; expect missing adapter failures.
2. Add provider-specific tests proving local uses only its configured URI and remote uses only the
   already selected resource config. Pass hostile URI/path/command-like identity strings and prove
   they remain opaque comparison values and never reach connection or command construction.
3. Implement the smallest adapters over existing libvirt lifecycle primitives. Return only typed
   identities and categories; redact provider exceptions through existing redaction boundaries.
4. Wire construction into local and remote composition without changing existing
   `ProviderRuntime.external_boot` behavior or advertising v1 before an authenticated service host
   is configured. Rerun both adapter files and relevant composition tests; expect all to pass.
5. Run `just test-verbose tests/providers/external_boot_authority
   tests/providers/local_libvirt/test_external_boot_authority.py
   tests/providers/remote_libvirt/test_external_boot_authority.py`; expect all tests to pass.
6. Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 4 paths
   as `feat(providers): adapt authority mutations to libvirt`.

**Acceptance:** Local and remote providers obey one contract and expose no caller-selected provider
coordinates; legacy and non-external provider behavior is unchanged.

## Task 5: Prove the complete authority boundary

**Files:** Create `tests/adversarial/test_external_boot_authority_journal.py`; update the design
spec only if implementation revealed a factual correction, never to weaken a failed proof.

**Interfaces:** Consume the public protocol, service, repository, and both adapters exactly as
implemented above; define no production API.

1. Write adversarial races covering two generations, stale retries with successful idempotency
   keys, later Run versus earlier completed Run, lost response at every commit point, restart with
   an unresolved call, conflict/source/target classification, release/teardown, and stable recovery
   ownership. Run the file with a controlled fault in the generation or head comparison and confirm
   at least one test fails; revert the fault.
2. Run all focused provider and migration commands from the spec; expect all tests to pass. Run
   `just lint` and `just type`; expect exit 0.
3. Run `just ci` bare; expect exit 0. Record environment-gated live tiers as not run unless their
   prerequisites are present; do not treat a skip as a live proof.
4. Re-read `git diff main...HEAD`, verify wrapper docs are unaffected, run `git diff --check`, and
   commit the explicit adversarial test path and any factual spec correction as
   `test(providers): prove authority takeover fencing`.

**Acceptance:** The branch proves all issue completion criteria, all threat-model controls, and the
same bounded semantics for local and remote adapters. No excluded subsystem or deployment promise
is introduced.

## Rollback and cleanup

Migration 0123 is additive. Before any deployment advertises v1, rollback may stop the authority
service and leave its head rows and journal files retained for audit; it must not delete or rewind
either. After advertisement, rollback to direct mutation is forbidden because it bypasses the
accepted fence. Failed and temporary test journals use pytest-managed temporary directories and
are cleaned by fixtures. Production journal repair is only exact-byte restoration by an audited
operator and is outside the runtime API.
