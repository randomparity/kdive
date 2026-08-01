# Investigation-Scoped Kernel Build Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one externally built and validated kernel upload be selected by multiple Runs and Systems in its Investigation through an expiring content-addressed `build_ref`.

**Architecture:** A new investigation-build catalog owns one immutable artifact generation. External completion publishes or converges on a generation; `runs.create` can atomically copy its build result; install jobs pin referenced objects while providers consume them; close and TTL reconciliation reclaim generations without crossing Investigation or generation boundaries.

**Tech Stack:** Python 3.14, psycopg/PostgreSQL forward-only SQL migrations, FastMCP/Pydantic, pytest, Ruff, ty, `just` guardrails.

## Global Constraints

- Frozen scope: `https://github.com/randomparity/kdive/issues/1519 + work-1519-20260801-d`.
- External upload is the only build lane; do not restore `runs.build` or retired build jobs.
- No remote-provider-specific behavior and no unrelated artifact classes.
- Lock order is Investigation → Run everywhere both are needed.
- `build_ref` is `<64 lowercase hex content digest>.<canonical lowercase UUID generation>`.
- `KDIVE_BUILD_ARTIFACT_RETENTION_DAYS` is days per generation, measured by the PostgreSQL clock; reuse does not refresh it.
- Errors expose no cross-Investigation existence oracle and every suggested next action must be directly callable.
- Guardrails: focused pytest throughout, then `just lint`, `just type`, `just schema-guard`, `just migration-order-check`, `just docs-check`, `just cli-verbs-check`, `just test`, and `just ci`.
- ADR index is not coupled; do not add an index row.

---

### Task 1: Persist immutable build generations

**Files:**
- Create: `src/kdive/db/schema/0095_investigation_builds.sql`
- Create: `src/kdive/services/runs/build_catalog.py`
- Modify: `src/kdive/domain/lifecycle/records.py`
- Modify: `src/kdive/db/repositories.py`
- Test: `tests/db/test_migrate.py`
- Test: `tests/services/runs/test_build_catalog.py`

**Interfaces:**
- Produces: `InvestigationBuild`, `BuildPublication`, `publish_or_reuse_build(conn, *, run, result, heads, retention) -> BuildPublication`, `resolve_build(conn, investigation_id, build_ref) -> InvestigationBuild | None`, and `parse_build_ref(value) -> tuple[str, UUID]`.
- Consumes: `BuildStepResult`, validated `HeadResult` values, existing repository models, `LockScope.INVESTIGATION` held by callers.

- [ ] **Step 1: Write migration tests that require the catalog shape**

Add assertions that migration 0095 creates `investigation_builds` with composite primary key `(investigation_id, generation)`, unique `(investigation_id, build_ref)`, checks for 64-hex digest and `active|reclaiming`, JSONB canonical/build-result documents, exact artifact-key/version JSON, and `expires_at`; adds nullable `runs.build_ref`; and indexes active digest lookup and expiry.

- [ ] **Step 2: Run the migration tests and verify RED**

Run: `uv run python -m pytest tests/db/test_migrate.py -q`

Expected: FAIL because migration 0095 and the new columns/table do not exist.

- [ ] **Step 3: Add migration 0095**

Use forward-only SQL with this contract:

```sql
CREATE TABLE investigation_builds (
    investigation_id uuid NOT NULL REFERENCES investigations(id),
    generation uuid NOT NULL,
    build_ref text NOT NULL,
    content_digest text NOT NULL,
    canonical_document jsonb NOT NULL,
    build_result jsonb NOT NULL,
    artifacts jsonb NOT NULL,
    target_kind text NOT NULL,
    build_profile jsonb NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'reclaiming')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, generation),
    UNIQUE (investigation_id, build_ref)
);
ALTER TABLE runs ADD COLUMN build_ref text;
```

Add the exact digest/build-ref checks and indexes described above; add the standard `updated_at` trigger.

- [ ] **Step 4: Write failing build-catalog unit/service tests**

Cover canonical JSON determinism, order-independent HEAD input, digest-plus-generation parsing, malformed references, active unexpired convergence, expired/reclaiming same-digest new generation, canonical mismatch fail-loud behavior, Postgres-clock expiry, and generation-scoped stored artifact keys/versions.

- [ ] **Step 5: Run focused catalog tests and verify RED**

Run: `uv run python -m pytest tests/services/runs/test_build_catalog.py -q`

Expected: FAIL on missing catalog module/types.

- [ ] **Step 6: Implement the catalog module and repository model**

Canonicalize with `json.dumps(document, sort_keys=True, separators=(",", ":"))`, hash UTF-8 with SHA-256, and format `f"{digest}.{generation}"`. Query `SELECT now()` once for publication/expiry decisions. Require callers to hold the Investigation lock; do not acquire Run inside this module.

- [ ] **Step 7: Run focused tests and schema guards**

Run: `uv run python -m pytest tests/db/test_migrate.py tests/services/runs/test_build_catalog.py -q`

Run: `just schema-guard`

Run: `just migration-order-check`

Run: `just lint`

Run: `just type`

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/db/schema/0095_investigation_builds.sql src/kdive/services/runs/build_catalog.py src/kdive/domain/lifecycle/records.py src/kdive/db/repositories.py tests/db/test_migrate.py tests/services/runs/test_build_catalog.py
git commit -m "feat: add investigation build catalog"
```

### Task 2: Publish external completions into the catalog

**Files:**
- Modify: `src/kdive/services/runs/complete_build.py`
- Modify: `src/kdive/services/runs/steps.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`
- Test: `tests/services/runs/test_complete_build.py`
- Test: `tests/mcp/lifecycle/test_complete_build_tool.py`

**Interfaces:**
- Consumes: Task 1 `publish_or_reuse_build` and `BuildPublication`.
- Produces: `BuildStepResult.build_ref`, `BuildStepResult.expires_at`, completion response `data.build_ref`, `data.expires_at`, `data.server_time`.

- [ ] **Step 1: Write failing completion tests**

Prove the final transaction takes Investigation then Run, publishes all validated kernel/initrd/vmlinux checksums, registers only winner artifacts with `owner_kind='investigations'`, records `runs.build_ref`, and returns the deadline contract. Concurrent identical completions must share the active generation while the loser uses winner refs and deletes only its own exact versions after commit; injected delete failure must leave objects for the existing prefix-orphan sweep.

- [ ] **Step 2: Run focused completion tests and verify RED**

Run: `uv run python -m pytest tests/services/runs/test_complete_build.py tests/mcp/lifecycle/test_complete_build_tool.py -q`

Expected: FAIL because completion remains Run-owned and returns no build reference.

- [ ] **Step 3: Extend `BuildStepResult` and publication finalization**

Add optional serialized fields:

```python
build_ref: str | None = None
expires_at: str | None = None
```

Change `_finalize_external_build` to acquire `LockScope.INVESTIGATION` before `LockScope.RUN`, re-read Run/window under both, call the catalog publisher, register only selected winner rows, and persist the selected result/ref. Keep legacy result parsing tolerant of absent fields.

- [ ] **Step 4: Implement loser cleanup and response fields**

Return one `BuildPublication` from the service. After commit, delete only the losing candidate's versions; log and leave failures to orphan repair. `_complete_envelope` and replay return the stored build reference/deadline plus database `server_time`.

- [ ] **Step 5: Run focused tests, including mutations and guardrails**

Run: `uv run python -m pytest tests/services/runs/test_complete_build.py tests/mcp/lifecycle/test_complete_build_tool.py -q`

Assert the recorded acquisition trace is Investigation then Run. Temporarily register loser rows
and confirm the ownership assertion fails; restore. Then run `just lint` and `just type`; all pass.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/services/runs/complete_build.py src/kdive/services/runs/steps.py src/kdive/mcp/tools/lifecycle/runs/complete_build.py tests/services/runs/test_complete_build.py tests/mcp/lifecycle/test_complete_build_tool.py
git commit -m "feat: publish reusable external builds"
```

### Task 3: Select reusable builds at Run creation

**Files:**
- Modify: `src/kdive/services/runs/admission.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/create.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/view.py`
- Test: `tests/services/runs/test_create_flow.py`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`
- Test: `tests/mcp/core/test_tool_wrapper_boundary.py`

**Interfaces:**
- Consumes: Task 1 `parse_build_ref` and `resolve_build`.
- Produces: `RunCreateRequest.build_ref`, `RunCreateResult.build_ref`, succeeded-at-create Run/build-step persistence, and agent-facing response routing.

- [ ] **Step 1: Write failing bound and unbound reuse tests**

Cover two distinct Systems selecting one generation, unbound create then bind, exact target-kind/profile compatibility, no upload manifest, succeeded build step copied verbatim, and idempotency replay. Rejections: malformed, missing, expired, reclaiming, cross-Investigation, target mismatch, and profile mismatch; assert no Run, hold, audit transition, or existence leak.

- [ ] **Step 2: Run create tests and verify RED**

Run: `uv run python -m pytest tests/services/runs/test_create_flow.py tests/mcp/lifecycle/test_runs_tools.py -q -k 'build_ref or reusable'`

Expected: FAIL because `build_ref` is not accepted.

- [ ] **Step 3: Implement transactional reuse selection**

Add `build_ref: str | None` to request/result/idempotency documents. Inside existing Investigation-locked create paths, resolve and validate before insertion. Extend `_insert_created_run` with optional selected build; when present insert state `succeeded`, refs, `runs.build_ref`, and a succeeded build step in the same transaction. Without it preserve `created` behavior byte-for-byte.

- [ ] **Step 4: Implement self-correcting errors and next actions**

Use `build_ref_not_found` for missing and cross-Investigation. Use `build_ref_incompatible` for safe target/profile mismatches. Use `build_ref_expired` with `expires_at`, Postgres `server_time`, and only `runs.create` in `suggested_next_actions`; docs must say retry without the reference before upload.

- [ ] **Step 5: Update wrapper and read-model contracts**

Add the digest-plus-UUID `Field` description and wrapper docstring to `runs.create`. Reuse success points to `runs.install`; ordinary success retains the external-upload sequence. Surface `build_ref`/`build_expires_at` in `runs.get` and `runs.list` data without exposing another Investigation.

- [ ] **Step 6: Run focused tests and mutation checks**

Run: `uv run python -m pytest tests/services/runs/test_create_flow.py tests/mcp/lifecycle/test_runs_tools.py tests/mcp/core/test_tool_wrapper_boundary.py -q`

Temporarily remove the Investigation predicate and verify the cross-tenant test fails; restore. Temporarily skip profile comparison and verify its test fails; restore.

Run: `just lint`

Run: `just type`

Expected: PASS after every mutation is restored.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/services/runs/admission.py src/kdive/mcp/tools/lifecycle/runs/create.py src/kdive/mcp/tools/lifecycle/runs/registrar.py src/kdive/mcp/tools/lifecycle/runs/common.py src/kdive/mcp/tools/lifecycle/runs/view.py tests/services/runs/test_create_flow.py tests/mcp/lifecycle/test_runs_tools.py tests/mcp/core/test_tool_wrapper_boundary.py
git commit -m "feat: reuse investigation builds at run creation"
```

### Task 4: Fence install consumption across expiry and reclaim

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/steps.py`
- Modify: `src/kdive/jobs/handlers/runs/install.py`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: catalog deadline/state and Run `build_ref`.
- Produces: Investigation→Run install admission, expiry rejection, and queued/running job fence consumed by Task 5 GC.

- [ ] **Step 1: Write failing install lifecycle tests**

Prove first install/restage before expiry enqueues under Investigation→Run locks; first install/restage after expiry fails with timestamps and `runs.create`; unchanged succeeded variant after expiry is a no-op; a queued job delayed beyond expiry still installs; failed jobs release the fence. Add a barrier test concurrent with complete-build proving no cyclic wait.

- [ ] **Step 2: Run install tests and verify RED**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q -k 'install and (build_ref or expiry or deadlock)'`

Expected: FAIL because install does not inspect catalog generation or take Investigation lock.

- [ ] **Step 3: Implement admission ordering and deadline check**

Resolve the Run before locking only to obtain Investigation id, then acquire Investigation and Run, re-read Run and step/job state, return existing no-op first, and otherwise require active/unexpired generation before enqueue/recycle. Keep provider consumption unchanged; the durable queued/running job row is the lease.

- [ ] **Step 4: Run focused install tests**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py tests/jobs/handlers/test_runs_install.py -q`

Expected: PASS.

Temporarily reverse complete-build to Run → Investigation and run the bounded
complete-build-versus-install barrier test; it must fail or time out. Restore the correct order,
then run `just lint` and `just type`; all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/steps.py src/kdive/jobs/handlers/runs/install.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat: pin reusable builds during install"
```

### Task 5: Reclaim generations safely

**Files:**
- Modify: `src/kdive/reconciler/cleanup/gc.py`
- Modify: `src/kdive/reconciler/loop.py`
- Test: `tests/reconciler/test_gc_investigation_artifacts.py`
- Test: `tests/reconciler/test_gc_expired_build_artifacts.py`
- Test: `tests/adversarial/test_investigation_build_races.py`

**Interfaces:**
- Consumes: catalog state, exact artifact version set, Run build refs, queued/running install jobs.
- Produces: generation-scoped close/TTL reclaim with resumable `reclaiming` state.

- [ ] **Step 1: Write failing GC and race tests**

Cover close-plus-grace, absolute expiry, live referencing Run deferral where applicable, queued/running install deferral, settled failure release, state recheck, per-version partial deletion retry, old/new same-digest generation isolation, and concurrent create/install/complete versus reclaim barriers. Keep legacy run-owned cases unchanged.

- [ ] **Step 2: Run GC tests and verify RED**

Run: `uv run python -m pytest tests/reconciler/test_gc_investigation_artifacts.py tests/reconciler/test_gc_expired_build_artifacts.py tests/adversarial/test_investigation_build_races.py -q`

Expected: FAIL because sweeps enumerate only Run-owned rows.

- [ ] **Step 3: Implement generation candidate selection and mark**

Under Investigation lock select eligible active generations, query referencing Runs/install jobs, and atomically mark only eligible rows `reclaiming`. Capture their exact artifact keys and version ids from the generation document; never glob or derive another generation.

- [ ] **Step 4: Implement resumable deletion and drain**

Delete exact retired versions in bounded batches. On any store failure keep `reclaiming`. After all versions drain, reacquire Investigation lock, require the same generation still reclaiming, delete its artifact rows and catalog row, and preserve legacy sweep behavior/markers until both legacy and generation work drains.

- [ ] **Step 5: Run focused tests and mutation checks**

Run the Task 5 command. Then temporarily remove the queued-job predicate and prove install/reclaim fails; restore. Temporarily delete by content digest rather than generation and prove old/new isolation fails; restore.

Run: `just lint`

Run: `just type`

Expected: PASS after every mutation is restored.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/reconciler/cleanup/gc.py src/kdive/reconciler/loop.py tests/reconciler/test_gc_investigation_artifacts.py tests/reconciler/test_gc_expired_build_artifacts.py tests/adversarial/test_investigation_build_races.py
git commit -m "feat: reclaim investigation build generations"
```

### Task 6: Agent-facing documentation, ADR ratification, and full proof

**Files:**
- Modify: `docs/adr/0531-investigation-scoped-kernel-builds.md`
- Modify: `docs/guide/reference/runs.md`
- Modify: `docs/guide/reference/artifacts.md`
- Modify: generated mirrors under `src/kdive/mcp/resources/_content/`
- Modify: `src/kdive/cli/commands/_generated_verbs.py`
- Modify: `CHANGELOG.md`
- Test: `tests/mcp/core/test_tool_docs.py`
- Test: `tests/cli/test_generated_json_params.py`

**Interfaces:**
- Consumes: completed Tasks 1–5 public behavior.
- Produces: installable agent contract, generated artifacts, Accepted ADR, and green repository gates.

- [ ] **Step 1: Write/extend documentation contract tests**

Assert `runs.create` describes build-ref format, same-Investigation scope, compatibility, deadline clock/unit/scope/consequence/recovery, and distinct upload versus reuse next actions. Assert completion/get expose the handle and deadline.

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/cli/test_generated_json_params.py -q`

Expected: FAIL until docs and generated CLI schema are updated.

- [ ] **Step 3: Update canonical docs and regenerate committed outputs**

Edit canonical guide pages, then run `just docs`, `just resources-docs`, and `just cli-verbs`.
Add a factual changelog entry. Do not describe `runs.build`.

- [ ] **Step 4: Mark ADR-0531 Accepted**

Change only its `## Status` value to `Accepted (2026-08-01)` now that every decision is implemented in this branch.

- [ ] **Step 5: Run focused and aggregate guardrails bare**

Run: `just lint`

Run: `just type`

Run: `just schema-guard`

Run: `just migration-order-check`

Run: `just docs-check`

Run: `just cli-verbs-check`

Run: `just test`

Run: `just ci`

Expected: every command exits 0 with zero warnings. Report Docker/live skips exactly; do not run live VM tiers because provider bytes and install behavior are unchanged.

- [ ] **Step 6: Verify tests bite**

Repeat the scoped mutations named in Tasks 2–5 one at a time, run their focused tests to observe RED, restore each change, and re-run focused tests GREEN. Record the commands/results in the eventual PR testing section.

- [ ] **Step 7: Commit**

```bash
git add docs/adr/0531-investigation-scoped-kernel-builds.md docs/guide/reference/runs.md docs/guide/reference/artifacts.md src/kdive/mcp/resources/_content src/kdive/cli/commands/_generated_verbs.py CHANGELOG.md tests/mcp/core/test_tool_docs.py tests/cli/test_generated_json_params.py
git commit -m "docs: publish reusable build contract"
```

## Self-Review

- Spec coverage: Tasks 1–5 cover identity/schema, publication, create reuse, install fencing, both GC paths, tenancy, expiry recovery, concurrency, and legacy compatibility; Task 6 covers every agent-facing and generated contract plus ratification.
- Placeholder scan: no TBD/TODO/“similar to” steps; every test and implementation action names exact files, behavior, commands, and expected state.
- Type consistency: `BuildPublication`, `InvestigationBuild`, `build_ref`, `expires_at`, `BuildStepResult`, and catalog helper names are introduced once in Task 1/2 and consumed consistently afterward.
- Resume facts: branch `feat/investigation-kernel-artifacts-1519`; base `main`; aggregate gate `just ci`; ADR index not coupled; no open review findings at plan authoring.
