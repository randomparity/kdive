# Fence capture artifact publication — implementation plan (#1952)

## Goal

Make traffic-capture publication part of the exact supervised job attempt so cancellation,
session loss, retry, and later reaping observe one durable closure predicate and cannot leave
unowned packet data.

The implementation keeps ADR-0558's provider-operation state and adds an orthogonal publication
state plus spool-disposal proof. Object-store conditional creation supplies the remote arbitration
point; PostgreSQL remains authority for current attempt, metadata registration, and closure.

**Tech stack:** Python 3.14, psycopg 3, PostgreSQL migrations, boto3 S3 API, pytest, MinIO.

## Global constraints

- Base branch: `main`; implementation branch: `feat/fence-capture-publication-1952`.
- Host architecture: `x86_64`; declared targets: `x86_64`, `ppc64le`; relationship: included.
- Protocol 4 is build-new-only. No protocol-3 data, work, or objects are migrated, preserved,
  reconciled, or cleaned up. Migration refuses a nonempty installation.
- Keep object-store I/O outside the Run advisory lock. Hold the per-job session fence through live
  provider execution and publication.
- `published` requires one truthful artifact row and capture object. `discarded` requires no
  artifact row or capture version and a retained operation-identity tombstone.
- A fully closed attempt is `(exited, published|discarded, spool_disposed)`.
- Unverified objects are never deleted or adopted. They retain `canceling`, block readiness and
  acknowledgment, and emit `capture_publication_object_identity_conflict`.
- No MCP tool, response, artifact bytes, sensitivity, retention, or agent-facing schema changes.
- Guardrails: focused pytest per task, then bare `just ci`; CI also invokes recipes individually.
  ADR status is not coupled to an index. Flip ADR-0559 from Proposed to Accepted only with the
  implementing code.

## Task 1: Admit atomic conditional creation at the object-store boundary

**Files**

- Modify `src/kdive/artifacts/storage.py`: conditional-write request/result and HEAD metadata.
- Modify `src/kdive/store/objectstore.py`: conditional create, precondition mapping, metadata read,
  and the live admission probe.
- Modify `src/kdive/processes/worker.py`: run admission before recovery/readiness.
- Modify `tests/store/test_objectstore.py`: request/response and malformed-store unit coverage.
- Modify `tests/processes/test_worker.py`: readiness failure/success wiring.
- Add `tests/integration/test_capture_publication_store_admission.py`: real MinIO concurrency proof.

**Interfaces**

- Add `ConditionalArtifactWriteRequest` with `key`, `data`, `metadata`, `sensitivity`, and
  `retention_class` fields; caller supplies a validated full key.
- Add `ConditionalCreateResult = StoredArtifact | ConditionalCreateConflict`.
- Add `ObjectStore.create_if_absent(request) -> ConditionalCreateResult`.
- Extend `HeadResult` with immutable `metadata: Mapping[str, str]` while preserving existing named
  fields and call sites.
- Add `ObjectStore.validate_conditional_create() -> None`; success leaves no probe version.

**TDD steps**

1. Write unit tests that send `IfNoneMatch="*"`, normalize 412/PreconditionFailed into
   `ConditionalCreateConflict`, preserve other `ClientError` values as categorized infrastructure
   failures, and return operation metadata from HEAD. Run:
   `uv run python -m pytest tests/store/test_objectstore.py -q`; expect new tests to fail on missing
   APIs before implementation.
2. Implement the minimal request/result types and store methods. Keep response parsing through
   `_StoreReply`; do not add boto stubs or a dependency. Rerun the command; expect all tests pass.
3. Add fake-store tests for admission outcomes: one winner/one conflict passes; double success,
   double conflict, missing version id, HEAD mismatch, delete failure, and residual probe version
   fail closed with actionable messages. Break winner counting once and confirm a test fails.
4. Implement `validate_conditional_create` with a random internal key, two concurrent zero-byte
   creates, exact-version cleanup in `finally`, and a final HEAD. Never log endpoint credentials or
   object-store exception text.
5. Add the MinIO integration test that synchronizes two real creates at one key and asserts one
   winner, one conflict, exact winner HEAD, deletion, and absence. Run the focused integration
   module against the existing disposable store fixture; expect pass or the fixture's documented
   environment skip, never a silent success over no assertions.
6. Wire worker startup before capture recovery/readiness and test that a failed probe prevents job
   claims. Run `uv run python -m pytest tests/processes/test_worker.py -q`.
7. Run `just lint`, `just type`, the three focused modules, and bare `just ci`; verify
   `git status --porcelain` contains only the intended tracked changes and no untracked files. Keep
   ADR-0559 Proposed because the publication state machine is not implemented yet. Commit, then
   require empty status:
   `feat(store): admit conditional capture publication`.

**Acceptance**

- Production never infers conditional-create semantics from versioning or API name.
- Worker readiness proves the configured store's overlap behavior and cleans its probe.
- Unsupported/degraded stores cannot claim jobs.

## Task 2: Add the protocol-4 publication state product

**Files**

- Add `src/kdive/db/schema/0113_capture_publication_fence.sql`.
- Modify `src/kdive/services/runs/worker_incarnations.py`: protocol constant 4.
- Modify `src/kdive/jobs/capture_operations/repository.py`: publication fields and transitions.
- Modify `src/kdive/jobs/queue.py`: combined closure gate before retry/current-link clearing.
- Add `tests/jobs/test_capture_publication_repository.py`.
- Add `tests/jobs/test_capture_publication_fresh_install.py`.
- Modify protocol expectations in `tests/jobs/test_capture_operation_repository.py`,
  `tests/jobs/test_capture_operation_cutover.py`, worker-fence tests, Compose lifecycle tests, and
  any exact protocol guard discovered with `rg -n 'protocol 3|fence_protocol = 3'`.

**Interfaces**

- `CaptureOperation` gains `publication_state`, `publication_object_key`, `publication_etag`,
  `publication_artifact_id`, `cleanup_capture_version_id`, `publication_tombstone_version`,
  `publication_started_at`, `publication_closed_at`, and `spool_disposed_at`.
- Migration 0113 adds a singleton protocol-4 installation-admission row carrying nullable
  database-clock `admitted_at` and a hash of the dedicated object-store namespace identity. Task 5
  owns the first write through a security-definer compare-and-set function; restarts only verify.
- Add repository functions:
  `begin_publication(conn, credential, operation_id, key)`,
  `begin_cancel_publication(conn, credential, operation_id, key)`,
  `record_capture_version(conn, credential, operation_id, version_id, etag)`,
  `commit_published(conn, credential, operation_id, artifact, audit_event)`,
  `record_cleanup_capture_version(conn, credential, operation_id, version_id)`,
  `commit_discarded(conn, credential, operation_id, tombstone_version)`, and
  `record_spool_disposed(conn, credential, operation_id)`.
- Every function returns the updated exact operation or raises the repository's existing refused
  transition error; replay accepts identical facts only.

**TDD steps**

1. Write migration tests that apply through 0112, seed each of worker incarnation, job, capture
   operation, and artifact independently, and assert 0113 rolls back without mutation. The empty
   case must install protocol 4 and cutoff
   `(operation_quiescent, publication_closed, complete) = (true, true, true)` with a fresh database
   clock. Run the new module and observe missing migration failures.
2. Implement 0113. Take the capture-protocol advisory fence, assert empty relevant tables, alter
   state/shape constraints, replace every exact protocol-3 security-definer function with protocol
   4, update the singleton cutoff, revoke direct grants, and grant only the functions each runtime
   role needs. Do not edit migration 0112.
3. Write repository tests for the full monotonic product, identical replay, conflicting replay,
   wrong credential, wrong attempt/current link, terminal job, `canceling` no-backward-edge,
   `published` row/etag identity, `discarded` tombstone identity, and spool-disposal completion.
4. Implement Python records and wrappers, then run both new modules. Break the current-link check
   and confirm its race test fails before restoring it.
5. Add queue tests showing a second attempt and current-link clearing remain barred for every
   incomplete product state and proceed only after spool disposal. Run
   `uv run python -m pytest tests/jobs/test_queue.py tests/jobs/test_worker.py -q`.
6. Update exact protocol expectations mechanically, without adding compatibility branches. Run
   `rg -n 'protocol 3|fence_protocol = 3' src tests deploy scripts docs/operating` and disposition
   every remaining hit as historical prose, deliberate negative fixture, or defect.
7. Run `just lint`, `just type`, focused database/job tests, `just migration-order-check`, and bare
   `just ci`; verify status contains only intended tracked changes and no untracked files. Commit,
   then require empty status: `feat(jobs): persist capture publication closure`.

**Acceptance**

- The database alone can tell whether provider execution, publication, and spool disposal closed.
- Protocol-3 binaries cannot register, authenticate, or claim on a protocol-4 installation.
- Nonempty installations fail rather than convert or preserve state.

## Task 3: Publish inside the supervised attempt

**Files**

- Add `src/kdive/jobs/capture_operations/publication.py`: publication coordinator and metadata.
- Modify `src/kdive/jobs/capture_operations/supervisor.py`: publisher callback and authority race.
- Modify `src/kdive/jobs/handlers/control/capture_traffic.py`: remove the old post-supervision
  `_store_capture` path and inject the coordinator.
- Modify `src/kdive/jobs/capture_operations/launcher.py`: expose exact spool identity/disposal.
- Add `tests/jobs/capture_operations/test_publication.py`.
- Modify `tests/jobs/capture_operations/test_supervisor.py` and
  `tests/jobs/handlers/control/test_capture_traffic_handler.py`.

**Interfaces**

- `CapturePublicationCoordinator.publish(conn, job, operation, snapshot, data) -> UUID` performs
  sequential-row adoption, conditional capture create, version journal, artifact/audit transaction,
  and terminal publication commit.
- `CaptureOperationSupervisor.execute(..., publisher: CapturePublisher) -> UUID | None` holds the
  job fence and monitors the lock session through publication.
- `LaunchedCapture.dispose_spool() -> bool` removes only its own mode-0700 operation directory and
  returns verified absence; recovery receives the same operation-derived path.

**TDD steps**

1. Write publication tests with synchronized fake store/repository stages before create, during
   create, after create before version journal, after journal before claim, during claim/audit, and
   after transaction before return. Confirm each new test fails against the old handler.
2. Implement the coordinator using the operation-id key and metadata
   `{operation-id, publication-kind=capture}`. Keep PUT outside the Run lock. Under the Run lock,
   atomically claim the artifact, audit, and commit `published`; do not retain the current
   best-effort discard semantics.
3. Refactor supervisor execution to call the publisher before leaving `_capture_job_fence` and race
   it against `_monitor_lock_session`. On authority loss cancel the publisher task, prevent further
   transitions on the dead connection, leave the operation durably nonterminal, and propagate to
   Task 4's recovery owner; do not acknowledge cancellation here.
4. Replace handler `_store_capture` with the injected publication callback. Preserve pcap
   validation, filtering, packet count, sensitivity, retention, response id, and short Run locks.
   Delete dead reconciliation helpers only after `rg` proves no callers.
5. Implement spool disposal after successful `published` state and before returning. Test success
   plus deletion failure. Break disposal ordering and confirm a test catches deletion before the
   artifact commit. Discarded spool closure belongs to Task 4.
6. Run focused handler, supervisor, publication, artifact discard/etag, queue, and worker tests;
   then `just lint`, `just type`, and bare `just ci`; verify status contains only intended tracked
   changes and no untracked files. Commit, then require empty status:
   `feat(worker): fence capture artifact publication`.

**Acceptance**

- Live publication cannot outlast its job fence or commit after `canceling`.
- A successful result has one matching row/object and no private spool.
- Authority loss leaves a recoverable nonterminal operation and never acknowledges cancellation.

## Task 4: Recover every publication crash boundary

**Files**

- Modify `src/kdive/jobs/capture_operations/publication.py`: cancellation arbitration/recovery.
- Modify `src/kdive/jobs/capture_operations/supervisor.py`: startup recovery and readiness summary.
- Modify `src/kdive/jobs/capture_operations/launcher.py`: operation-derived spool recovery path.
- Modify `tests/jobs/capture_operations/test_publication.py`.
- Modify `tests/jobs/capture_operations/test_supervisor.py`.
- Modify `tests/jobs/test_worker_main.py` and Compose lifecycle live fixtures as required.

**Interfaces**

- `recover_publication(conn, store, operation) -> CaptureOperation` handles `pending`, `publishing`,
  and `canceling` idempotently and returns only after publication plus spool closure.
- `PublicationObjectIdentity` parses HEAD metadata into exact operation id and `capture|tombstone`
  kind; mismatch raises a stable `CapturePublicationIdentityConflict` carrying only operation id,
  key, and reason code.

**TDD steps**

1. Seed and fail each durable boundary named by ADR-0559: pending/key-null, key journaled, ambiguous
   create, capture version journaled, cleanup version journaled, tombstone durable, published
   transaction committed, and terminal publication with spool present. Confirm recovery tests fail.
2. Implement `pending -> canceling` key derivation, conditional tombstone creation, HEAD identity
   parsing, capture-version journal-before-delete, exact-version absence verification, tombstone
   adoption, `discarded` commit, and spool disposal.
3. Add the delayed conditional-create race: release the database fence to replacement recovery,
   retain the tombstone, then let the old create evaluate and assert precondition failure. Add the
   crash-after-tombstone test and prove the same version is adopted without deleting it.
4. Add lost-delete-response recovery: persist the capture version, issue/delete ambiguously, and
   prove every resume verifies that exact version before tombstoning. HEAD current-key absence is
   insufficient.
5. Add corrupt-shape seeds: wrong operation id, unknown kind, and nonzero tombstone. Assert the
   object remains untouched, state remains `canceling`, readiness/acknowledgment stay barred, and
   only `capture_publication_object_identity_conflict` plus operation/key is logged.
6. Add success/cancel/replacement spool tests. A failed unlink or unverifiable directory retains
   null `spool_disposed_at`; verified absence is idempotent success.
7. Run publication, supervisor, worker-main, and Compose lifecycle modules. Run `just lint`,
   `just type`, `just test-changed`, and bare `just ci`; verify status contains only intended
   tracked changes and no untracked files. Commit, then require empty status:
   `fix(worker): recover capture publication boundaries`.

**Acceptance**

- Every crash resumes monotonically without reopening publication.
- No unverified object is deleted or adopted.
- Worker readiness positively proves all recoverable local operations fully closed.

## Task 5: Align fresh-install deployment and complete verification

**Files**

- Modify protocol-dependent fresh-install configuration/tests under `deploy/helm/kdive/`,
  `scripts/live-stack/`, and `deploy/ansible/` only where current protocol constants require it.
- Add `scripts/verify-fresh-publication-install.py`: pre-start database and object-store namespace
  admission for build-new-only deployment.
- Add `tests/scripts/test_verify_fresh_publication_install.py`.
- Modify operator documentation that currently instructs protocol-3 cutover or upgrade.
- Modify `docs/adr/0559-fence-capture-artifact-publication.md` status only if not already flipped.

**Interfaces**

- No new operator migration command. Fresh setup applies the full schema to an empty database and
  starts protocol-4 workers only after store admission and recovery.
- Fresh deployment supplies a dedicated object-store bucket/namespace. Before the first worker
  or any other application writer receives bucket credentials, the admission job is their sole
  holder. `verify-fresh-publication-install.py` records `admitting` under the protocol advisory
  lock, requires application database tables and the configured bucket to be empty, performs the
  store conditional-create capability probe, rechecks bucket emptiness excluding only its exact
  probe versions, and compare-and-sets the marker to `admitted`. Worker credentials and replicas
  become available only after that commit. Later restarts verify the admitted namespace hash and
  do not require the now-live namespace to remain empty.

**TDD steps**

1. Write script tests for empty database plus empty bucket admission, either side nonempty, store
   listing failure, concurrent first admission, marker replay, mismatch against a reused namespace,
   and an object injected between initial listing and marker finalization. The injected object must
   leave the marker `admitting`, never `admitted`; retry fails closed until the operator supplies a
   new empty namespace. Confirm failures delete only exact probe versions and never user objects or
   database rows. Implement the script against Task 2's protocol-4 installation marker.
2. Update fresh-install render/shape tests to require protocol 4, the pre-start admission step, a
   dedicated bucket/namespace, exclusive admission credentials, no worker secret/replicas before
   `admitted`, and rejection of data-preservation paths. Run focused Helm, Compose, Ansible, script,
   and live-workflow-shape modules; observe old expectations fail.
3. Update only the production manifests/scripts/docs required for fresh installation. Remove or
   replace protocol-3 upgrade instructions exposed as current guidance; do not add shims.
4. Run focused deployment tests, `just lint-shell`, `just lint-ansible`, `just test-ansible`,
   `just lint-workflows`, documentation guards, and `just adr-status-check`. Flip ADR-0559 to
   Accepted in this final implementation commit. Commit:
   `feat(deploy): install capture publication protocol 4`.
5. Run fresh-namespace admission against a disposable empty database/bucket and report their exact
   identities in public-safe form plus the marker result. Then run the live MinIO conditional-
   create integration proof and report the exact arm/result. Live
   provider tiers are not required because provider execution and MCP behavior do not change.
6. Run bare `just ci`. Then run `git status --porcelain`; any output, including untracked files,
   invalidates the green claim. Review `git diff main...HEAD` for naming, complexity, dead helpers,
   and accidental protocol compatibility.

**Acceptance**

- A fresh supported deployment starts only protocol-4 workers against an admitted object store.
- No application writer can access the new namespace between its final emptiness proof and the
  durable `admitted` marker.
- All fault, database, worker, deployment, and repository guardrails pass without warnings.
- The worktree is clean and ADR-0559 is Accepted only with its implementation present.
