# Worker-crash artifact-use fences implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every exact artifact version pinned for the full provider attempt and recover a crashed worker's pin only from role-separated immutable termination evidence.

**Architecture:** PostgreSQL protects incarnation, use, and recovery transitions with role-gated functions and per-incarnation credentials. Compose and Kubernetes lifecycle authorities pre-register exact runtime identities before workers claim jobs; the database rejects old/unregistered workers. Install cancellation waits for the provider thread, while process death leaves the committed use for evidence-backed recovery.

**Tech Stack:** Python 3.14, psycopg 3, PostgreSQL migrations/functions/triggers, asyncio, Docker Engine API, Helm/Kubernetes APIs, pytest, Ruff, ty, `just`.

## Global constraints

- Frozen scope: issue #1803, token `scope-1803-7f7a1ef9-20260802`; interaction remains interactive.
- Base branch: `main`; feature branch: `feat/worker-crash-artifact-fences-1803`.
- Guardrails: each task's named focused pytest command per red/green cycle and `just ci` before every task commit.
- Preserve `main` migrations 0095–0097 byte-for-byte; the #1803 migration tail starts at 0098 with unique versions.
- No new dependency. Exact versions remain object-store deletion inputs; no lease/heartbeat/absence recovery.
- Identity ≤512 bytes; actor ≤255; evidence ≤1024; reason ≤512; list page ≤100; pass ceiling 1,000 database rows.
- Only lifecycle authority creates/binds/activates an incarnation. Worker authenticates it and cannot terminate it.
- Every protected lookup joins use → generation → investigation → project before mutation.
- Unsupported host-root Docker, force-delete, and manual-finalizer paths fail closed and may strand pins.
- ADR-0533 and `docs/superpowers/specs/2026-08-02-worker-crash-artifact-fences-design.md` govern implementation.

---

### Task 1: Reconcile the migration tail and install database authority

**Files:**
- Remove with `gio trash`: `src/kdive/db/schema/0096_investigation_build_safety.sql`, `src/kdive/db/schema/0097_investigation_build_use_leases.sql`, `src/kdive/db/schema/0098_investigation_build_tombstones.sql`
- Rename/rewrite: preserved #1803 migrations as unique `0098`–`0105` files under `src/kdive/db/schema/`
- Modify: `tests/db/test_migrate.py`
- Modify: `tests/db/test_migration_0091_system_object_sweep_cursors.py`
- Create: `tests/db/test_worker_fence_authority.py`

**Interfaces:**
- Produces SQL roles `kdive_server`, `kdive_worker`, `kdive_reconciler`, `kdive_lifecycle_witness`.
- Produces guarded functions `register_worker_incarnation`, `authenticate_worker_incarnation`, `terminate_worker_incarnation`, `acquire_investigation_build_use`, `release_investigation_build_use`, and `recover_investigation_build_use`.
- Protected tables retain no direct runtime-role mutation grants.
- Claim-trigger activation is deliberately deferred to Task 3, after credential-aware worker code exists.

- [ ] **Step 1: Write failing migration and role tests**

Add tests that assert migration versions are unique and end in the new monotonic tail. Create distinct
LOGIN principals with only one intended non-login runtime-role membership, plus an unprivileged login,
and open a fresh connection as each for guarded-function checks. Use `SET LOCAL ROLE` only for direct
table-grant assertions; it cannot model `session_user`:

```python
@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        ("kdive_worker", "direct_terminate", False),
        ("kdive_lifecycle_witness", "register", True),
        ("kdive_worker", "terminate_function", False),
        ("kdive_reconciler", "direct_delete_use", False),
    ],
)
def test_worker_fence_role_matrix(role: str, operation: str, allowed: bool, role_dsn) -> None:
    with psycopg.connect(role_dsn(role)) as role_conn:
        assert _login_operation_succeeds(role_conn, operation) is allowed
```

- [ ] **Step 2: Run the red tests**

Run: `uv run python -m pytest tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_worker_fence_authority.py -q`

Expected: failure from duplicate migration versions and missing roles/functions.

- [ ] **Step 3: Build a unique immutable migration tail**

Keep `main` 0095–0097 unchanged. Consolidate use rows, recovery audit, bounds/indexes/cursors,
incarnation protocol and credential hash, role creation, revokes/grants, and guarded functions into
versions 0098–0105. Do not activate the incompatible claim trigger in this task. Security-definer
functions must set an empty `search_path`, schema-qualify objects, validate bounds, and check
`session_user` role membership.

Core protected shape:

```sql
CREATE TABLE worker_incarnations (
    incarnation text PRIMARY KEY,
    authority_kind text NOT NULL,
    authority_binding jsonb NOT NULL,
    fence_protocol integer NOT NULL,
    credential_hash bytea NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'active',
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminated_at timestamptz,
    outcome text
);
REVOKE INSERT, UPDATE, DELETE ON worker_incarnations FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON investigation_build_uses FROM PUBLIC;
```

- [ ] **Step 4: Run migration tests green**

Run the Step 2 command.

Expected: all pass; permission-denied cases roll back their transaction before the next assertion.

- [ ] **Step 5: Run repository guardrails and commit**

Run: `just ci`

Commit explicit migration/test paths with: `feat: enforce worker fence database authority`

---

### Task 2: Bind worker operations to authority-minted incarnation credentials

**Files:**
- Modify: `src/kdive/services/runs/worker_incarnations.py`
- Modify: `src/kdive/services/runs/build_use.py`
- Modify: `src/kdive/processes/worker_incarnation.py`
- Modify: `src/kdive/processes/worker.py`
- Modify: `tests/services/runs/test_worker_incarnations.py`
- Modify: `tests/services/runs/test_build_use.py`
- Modify: `tests/processes/test_worker_incarnation.py`
- Modify: `tests/processes/test_worker.py`

**Interfaces:**
- `register_worker_incarnation(conn, incarnation, authority_kind, binding, credential_hash, fence_protocol) -> WorkerIncarnation` is lifecycle-authority only.
- `authenticate_worker_incarnation(conn, credential: SecretStr) -> WorkerIncarnation` is worker-only and returns the derived holder.
- `acquire_build_use(conn, run_id, *, job_id, attempt, incarnation_credential) -> UUID | None` never accepts holder identity.
- `release_build_use(conn, use_id, *, incarnation_credential) -> bool` deletes only an exact credential-derived holder/job/attempt match.

- [ ] **Step 1: Add red ownership and replay tests**

Cover identical authority replay, conflicting binding/hash/protocol replay, worker registration denial, wrong credential, cross-worker release, terminated authentication, and acquisition after claim replacement.

```python
released = await release_build_use(
    worker_a_conn,
    worker_b_use_id,
    incarnation_credential=worker_a_credential,
)
assert released is False
assert await _use_exists(admin_conn, worker_b_use_id)
```

- [ ] **Step 2: Run focused red tests**

Run: `uv run python -m pytest tests/services/runs/test_worker_incarnations.py tests/services/runs/test_build_use.py tests/processes/test_worker_incarnation.py tests/processes/test_worker.py -q`

Expected: failures from worker-side registration and caller-supplied ownership.

- [ ] **Step 3: Replace service SQL with guarded function calls**

Use constant-time credential verification inside PostgreSQL (`digest`/fixed hash comparison), return only public incarnation facts, and never log or persist the plaintext credential. Remove the worker's registration path; worker startup authenticates the pre-existing active identity before constructing `Worker`.

- [ ] **Step 4: Run focused tests green and prove the tests bite**

Run the Step 2 command. Then temporarily bypass the credential predicate in the SQL function, run the cross-worker test and observe failure, restore the predicate, and rerun green.

- [ ] **Step 5: Run `just ci` and commit**

Commit explicit service/process/test paths with: `security: bind build uses to worker credentials`

---

### Task 3: Reject old or unregistered workers at the database claim boundary

**Files:**
- Create: `src/kdive/db/schema/0106_worker_fence_protocol_claim.sql`
- Modify: `src/kdive/jobs/queue.py`
- Modify: `src/kdive/jobs/worker.py`
- Modify: `src/kdive/processes/worker.py`
- Modify: `tests/jobs/test_queue.py`
- Modify: `tests/jobs/test_worker.py`
- Modify: `tests/jobs/test_worker_main.py`

**Interfaces:**
- `dequeue(conn, worker_id, *, incarnation_credential: SecretStr, lease=DEFAULT_LEASE, accepted_lanes=DEFAULT_DISPATCH_LANES) -> Job | None` claims only through the guarded SQL function.
- Current protocol is one named constant shared by registration/startup tests, not a configurable downgrade.

- [ ] **Step 1: Add failing claim tests**

Test current active credential succeeds; missing, malformed, wrong, terminated, and old-protocol identities leave the job queued. Test a direct old-style `UPDATE jobs SET state='running'` under the worker role is denied.

- [ ] **Step 2: Run red queue tests**

Run: `uv run python -m pytest tests/jobs/test_queue.py tests/jobs/test_worker.py tests/jobs/test_worker_main.py -q`

Expected: old/unregistered claims still succeed or the new API is absent.

- [ ] **Step 3: Route every claim through the database protocol gate**

Add migration 0106 only after the credential-aware startup/dequeue code and red tests are present. Its
trigger denies direct old-style running transitions. The guarded claim function resolves the
incarnation from the credential, verifies `active` plus the exact protocol, then performs the existing
`FOR UPDATE SKIP LOCKED` claim. Preserve FIFO, lane selection, attempt charging, and lease semantics.

- [ ] **Step 4: Run queue tests and mutation check**

Run the Step 2 command and temporarily change one fixture to protocol `current - 1`; verify its claim test fails before restoring it.

- [ ] **Step 5: Run `just ci` and commit**

Commit with: `security: reject workers without fence protocol`

---

### Task 4: Retain the use fence across provider-thread cancellation

**Files:**
- Modify: `src/kdive/jobs/handlers/runs/install.py`
- Modify: `tests/jobs/handlers/test_runs_install.py`
- Modify: `tests/reconciler/test_gc_expired_build_artifacts.py`

**Interfaces:**
- `_run_install_step` receives the exact use/credential context and does not release it until `installer.install` has returned.
- Cancellation is re-raised only after the supervised thread task completes and run-step abandonment executes.

- [ ] **Step 1: Run the preserved failing cancellation proof**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py::test_cancelled_install_waits_for_provider_thread_before_abandoning -q`

Expected: FAIL because the task finishes/abandons before the provider thread is released.

- [ ] **Step 2: Supervise the provider task explicitly**

Use a cancellation-resistant drain around the thread-backed task. Every cancellation is recorded while
the same shielded task continues to be awaited; cleanup and the outer use release run only after the
thread is done:

```python
async def _wait_through_cancellation(task: asyncio.Task[object]) -> bool:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled

provider_task = asyncio.create_task(asyncio.to_thread(installer.install, request))
try:
    cancelled = await _wait_through_cancellation(provider_task)
except Exception:
    cleanup = asyncio.create_task(abandon_run_step_best_effort(conn, run_id, "install"))
    await _wait_through_cancellation(cleanup)
    raise
if cancelled:
    cleanup = asyncio.create_task(abandon_run_step_best_effort(conn, run_id, "install"))
    await _wait_through_cancellation(cleanup)
    raise asyncio.CancelledError
```

Acquire the build use immediately before this block and release in an outer `finally`, so process death
preserves the separately committed row. Add a test that cancels twice while the provider remains blocked
and asserts neither abandonment nor use release occurs until thread exit.

- [ ] **Step 3: Run cancellation, overlap, and GC tests green**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py tests/reconciler/test_gc_expired_build_artifacts.py -q`

Expected: all pass, including two overlapping attempts remaining pinned independently.

- [ ] **Step 4: Verify the test bites**

Temporarily remove `asyncio.shield`, observe the focused cancellation proof fail, restore, and rerun green.

- [ ] **Step 5: Run `just ci` and commit**

Commit with: `fix: retain build use through install cancellation`

---

### Task 5: Make Compose lifecycle authority evidence-preserving

**Files:**
- Modify: `src/kdive/processes/compose_worker_lifecycle.py`
- Modify: `src/kdive/processes/docker_death_api.py`
- Modify: `docker-compose.yml`
- Modify: `deploy/compose/README.md`
- Modify: `justfile`
- Modify: `tests/processes/test_compose_worker_lifecycle.py`
- Modify: `tests/processes/test_docker_death_api.py`
- Modify: `tests/compose/test_compose_config.py`
- Modify: `tests/compose/test_compose_lifecycle_recipe.py`

**Interfaces:**
- Gate creates without start, generates nonce/credential, binds full container ID, persists registration with witness DSN, then starts.
- Stop/recreate/remove persist exact terminal outcome before Docker deletion.
- No runtime worker receives Docker socket or witness/migration DSN.

- [ ] **Step 1: Add failing create/SIGKILL/recreate/outage/bypass tests**

Assert ordering with a call log:

```python
assert calls == ["create", "inspect-full-id", "register", "start"]
assert stop_calls == ["stop", "inspect-terminal", "terminate", "remove"]
```

Database failure must omit `start` on create and omit `remove` on teardown. Raw compose worker lifecycle must be structurally unavailable from documented recipes.

- [ ] **Step 2: Run red Compose tests**

Run: `uv run python -m pytest tests/processes/test_compose_worker_lifecycle.py tests/processes/test_docker_death_api.py tests/compose -q`

- [ ] **Step 3: Implement exact ordering and credential wiring**

Use the witness-specific DSN setting, inject only the worker DSN plus one-time incarnation credential, validate a full 64-hex container ID, and serialize lifecycle operations with the existing gate lock. Bound Docker API reads and errors; absence never terminates.

- [ ] **Step 4: Run focused tests and executable Docker proof where available**

Run Step 2. If Docker is reachable, run the existing Compose SIGKILL proof bare and record its result; otherwise record the exact Docker command showing unavailability and continue with structural tests.

- [ ] **Step 5: Run `just ci` and commit**

Commit with: `feat: preserve compose worker termination evidence`

---

### Task 6: Pre-register Kubernetes identities and fence finalizer removal

**Files:**
- Create: `src/kdive/processes/kubernetes_credential_broker.py`
- Create: `src/kdive/processes/kubernetes_credential_init.py`
- Modify: `src/kdive/processes/kubernetes_termination_witness.py`
- Modify: `src/kdive/processes/reconciler.py`
- Create: `deploy/helm/kdive/templates/service-worker-credential-broker.yaml`
- Create: `deploy/helm/kdive/templates/networkpolicy-worker-credential-broker.yaml`
- Modify: `deploy/helm/kdive/templates/statefulset-worker.yaml`
- Modify: `deploy/helm/kdive/templates/deployment-reconciler.yaml`
- Modify: `deploy/helm/kdive/templates/deployment-server.yaml`
- Modify: `deploy/helm/kdive/templates/worker-death-rbac.yaml`
- Modify: `deploy/helm/kdive/templates/_helpers.tpl`
- Modify: `deploy/helm/kdive/values.yaml`
- Create: `tests/processes/test_kubernetes_credential_broker.py`
- Create: `tests/processes/test_kubernetes_credential_init.py`
- Modify: `tests/processes/test_kubernetes_termination_witness.py`
- Modify: `tests/helm/test_helm_render.py`

**Interfaces:**
- Controller registers the exact Pending Pod UID; an init client uses a Pod-UID-bound projected token to idempotently deliver and acknowledge the encrypted credential envelope before worker start.
- Reconciler hosts a bounded internal TLS broker on a dedicated ClusterIP Service; it is not exposed through MCP/HTTP ingress.
- Broker frames are length-prefixed JSON capped at 16 KiB requests and 4 KiB responses with a 5-second database-clock operation deadline; secrets are never logged.
- Init atomically writes the credential with mode 0400 to a memory-backed `emptyDir`, fsyncs it, then retries acknowledgment until the durable marker returns success.
- Witness terminates only an existing lifecycle-registered UID and patches only its finalizer with UID/resourceVersion/binding tests.
- Truly unregistered terminal Pods remain finalized and cannot authorize recovery.

- [ ] **Step 1: Add failing controller/witness tests**

Cover pre-start registration, never-started-but-registered terminal Pod, truly unregistered terminal
Pod, invalid/audience-mismatched/unbound tokens, duplicate delivery before acknowledgment, dropped
delivery and acknowledgment responses, durable acknowledgment without redelivery, broker frame/timeout
bounds, TLS failure, atomic tmpfs mode/rename, envelope clearing after acknowledgment/termination, UID
replacement, rollout, scale-down, API/database failure, ordinal ceiling decrease, and exact JSON Patch
tests. Broker/client tests live in their dedicated files; witness state tests remain separate.

- [ ] **Step 2: Run red Kubernetes/Helm tests**

Run: `uv run python -m pytest tests/processes/test_kubernetes_credential_broker.py tests/processes/test_kubernetes_credential_init.py tests/processes/test_kubernetes_termination_witness.py tests/helm/test_helm_render.py -q`

- [ ] **Step 3: Implement bounded controller phases**

Use explicit states: observe fixed ordinal → validate UID/finalizer → ensure authority registration;
then init delivery → TokenReview with fixed audience → live UID/resource-version read → decrypt exact
envelope → atomic mode-0400 tmpfs write/fsync/rename → authenticated acknowledgment with repeated checks
→ atomically set acknowledged marker and clear envelope → worker gate. Delivery before acknowledgment is
idempotent for the same live UID; after acknowledgment delivery is refused and repeated acknowledgment
returns success without secret material. Another UID is refused.
Terminal processing is observe → verify existing binding → persist termination and clear envelope →
patch exact finalizer. Each pass handles at most the configured count capped at 1,000 and leaves
retryable state on failure. Ordinal reuse cannot overwrite a credential because registration and every
delivery/acknowledgment compare the exact UID.

- [ ] **Step 4: Minimize RBAC and prove rendering**

Grant the controller fixed-namespace Pod get/list, TokenReview create, and resource-version-fenced Pod
patch. Mount the short-lived fixed-audience projected token only in the init container; do not mount it
in the worker. Wire the init command, memory-backed `emptyDir`, dedicated ClusterIP Service, operator-
supplied TLS certificate/key/CA settings, readiness shutdown, and a NetworkPolicy allowing only worker
Pods to the broker port. Store the envelope key and TLS private key only in the reconciler deployment.
No credential Secret API permission is required. Keep the worker unable to read Pods, tokens, or other
credentials. Run Step 2 green and server-side Helm rendering if a cluster API is configured.

- [ ] **Step 5: Run `just ci` and commit**

Commit with: `feat: preserve kubernetes worker termination evidence`

---

### Task 7: Complete bounded tenant-safe recovery, deployment contracts, and proofs

**Files:**
- Modify: `src/kdive/mcp/tools/ops/build_uses.py`
- Modify: `src/kdive/reconciler/cleanup/gc.py`
- Modify: `src/kdive/mcp/assembly/app.py`
- Modify: `src/kdive/mcp/assembly/tool_registration.py`
- Modify: `src/kdive/config/core_settings.py`
- Modify: `src/kdive/config/external_env.py`
- Modify: generated CLI/reference files only via repository generators
- Modify: `tests/mcp/ops/test_build_use_recovery.py`
- Modify: `tests/reconciler/test_gc_expired_build_artifacts.py`
- Modify: `tests/config/test_worker_death_settings.py`
- Modify: `tests/mcp/core/test_tool_docs.py`
- Modify: Compose/Helm install, upgrade, RBAC, and recovery documentation already in the branch

**Interfaces:**
- Recovery tool lists ≤100 project-authorized rows with stable cursor and returns exact next action.
- Reconciler recovers one exact use via the role-gated function and immutable evidence.
- GC cursor processes ≤configured count capped at 1,000 and treats every use as a pin regardless of time.
- Wrapper docstrings state unit, database clock, scope, violation consequence, and recovery action for every limit.

- [ ] **Step 1: Add failing tenancy, audit, bounds, and GC-race tests**

Cover cross-project use IDs, missing/active/mismatched evidence, exact immutable audit tuple, 100-row pagination, 1,000-row hard ceiling, concurrent use-versus-termination lock ordering, last-use GC, object-store failure, and exact-version deletion.

- [ ] **Step 2: Run focused red tests**

Run: `uv run python -m pytest tests/mcp/ops/test_build_use_recovery.py tests/reconciler/test_gc_expired_build_artifacts.py tests/config/test_worker_death_settings.py tests/mcp/core/test_tool_docs.py -q`

- [ ] **Step 3: Implement the minimum recovery and configuration surface**

Keep MCP wrappers thin. Resolve project authorization before returning whether a use exists. Call the reconciler SQL function for audit+delete. Validate all setting bounds at startup and expose role-specific DSNs only to their owning processes. Regenerate artifacts with the existing `just` recipes/generator scripts before checking them.

- [ ] **Step 4: Update operator contracts and upgrade ordering**

Document stop old workers → migrate roles/protocol → rotate distinct credentials → start witnesses → start current workers → verify → resume. State that rollback cannot restore old claiming and that bypasses retain pins. Ensure wrapper docstrings and `Field` descriptions name all returned fields, bounds, database reference clock, refusal consequence, and next tool.

- [ ] **Step 5: Run focused tests, then full gate**

Run Step 2, then run `just ci` bare. Both must pass without warnings or skips that CI would treat as required failures.

- [ ] **Step 6: Run available end-to-end proofs**

Detect Docker/Kubernetes availability with their operator commands. Run the supported Compose SIGKILL/recreate/database-outage proof when Docker is available and the Helm server-side-render proof when Kubernetes is configured. Record every arm run or the exact verified environmental blocker; do not infer availability from a unit-test skip.

- [ ] **Step 7: Commit**

Commit explicit code, test, generated, deployment, and documentation paths with: `feat: recover worker artifact fences safely`

---

## Completion checkpoint

- [ ] Every task checkbox is complete and each task commit passed `just ci`.
- [ ] `git diff --check` and `git status --short --untracked-files=all` show no surprise.
- [ ] The implementation matches ADR-0533's authority matrix and the spec's threat boundaries.
- [ ] Durable resume facts: branch `feat/worker-crash-artifact-fences-1803`, base `main`, guardrail `just ci`, current phase `build`, open findings `none` after design review.
