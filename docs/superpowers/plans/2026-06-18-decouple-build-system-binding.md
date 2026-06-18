# Decouple build submission from a provisioned system — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (these tasks are
> tightly coupled through the `runs` domain/admission and share state, so they run inline in one
> session, not as independent parallel subagents). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let `runs.create` submit a build against a declared resource kind without holding a
provisioned System, binding a System at a new `runs.bind` step before install.

**Architecture:** `runs.system_id` becomes nullable; a new `runs.target_kind` (NOT NULL) records
the committed resource kind. The build resolves its builder from `run.target_kind` instead of the
System join. `runs.bind` reuses the create-time System admission (factored into a shared helper)
plus a kind-match contract. `install`/`boot` reject an unbound Run.

**Tech Stack:** Python 3.14, psycopg (async), FastMCP, pytest, `just` recipes, `uv`.

**Spec:** [`../specs/2026-06-18-decouple-build-system-binding.md`](../specs/2026-06-18-decouple-build-system-binding.md) · **ADR:** [ADR-0169](../../adr/0169-decouple-build-system-binding.md)

## Global Constraints

- Guardrails before every commit: `just lint`, `just type` (whole tree, src+tests), `just test`
  (focused subset during TDD; full `just ci` before first push). Zero warnings.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only. ≤100 lines/function,
  cyclomatic ≤8, ≤5 positional params. Google-style docstrings on non-trivial public APIs.
- Every tool returns a `ToolResponse`; failures carry the most specific `ErrorCategory` and never
  invent new category strings. Pick reasons from the spec's error table.
- New MCP tool requires **three** registrations or the full suite fails outside touched dirs:
  the registrar, `tests/mcp/test_tool_docs.py`, and `src/kdive/mcp/exposure.py` `PUBLIC_TOOLS`
  (+ its RBAC mapping).
- Doc-style: plain prose; never "Sprint"; avoid "critical/crucial/essential/comprehensive/robust/
  elegant". Conventional Commits; `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Migrations auto-discover by glob `schema/NNNN_*.sql`; next free is `0042`.

## File map

- Create: `src/kdive/db/schema/0042_decouple_run_system_binding.sql`
- Modify: `src/kdive/domain/lifecycle/__init__.py` (Run model: `system_id` optional, add
  `target_kind`)
- Modify: `src/kdive/db/repositories.py` (RUNS insert/get: new column, nullable system_id)
- Modify: `src/kdive/providers/core/resolver.py` (add `runtime_for_kind` / `resolve` use)
- Modify: `src/kdive/jobs/handlers/runs_build.py` (builder from `run.target_kind`)
- Modify: `src/kdive/services/runs/admission.py` (bound/unbound paths; factor
  `_admit_system_for_run`; inject resolver)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/create.py` + `registrar.py` (optional params,
  resolver wiring)
- Create: `src/kdive/services/runs/bind.py` (`bind_run` service) — or extend admission module
- Create: `src/kdive/mcp/tools/lifecycle/runs/bind.py` (tool handler) + registrar wiring
- Modify: `src/kdive/mcp/tools/lifecycle/runs/steps.py` (install/boot unbound guard)
- Modify: `src/kdive/jobs/handlers/runs_install.py`, `runs_boot.py` (defensive null-System guard)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/cancel.py` (NULL-system tolerance)
- Modify: `src/kdive/mcp/tools/ops/inventory.py` (`_system_data` add `kind`) +
  `src/kdive/mcp/tools/lifecycle/systems/view.py` / list (`kind` field)
- Modify: `src/kdive/mcp/exposure.py`, `tests/mcp/test_tool_docs.py` (register `runs.bind`)
- Tests under `tests/` mirroring the package tree; races under `tests/adversarial/`.

---

### Task 1: Migration 0042 + domain model

**Files:**
- Create: `src/kdive/db/schema/0042_decouple_run_system_binding.sql`
- Modify: `src/kdive/domain/lifecycle/__init__.py` (`Run.system_id: UUID | None`, add
  `target_kind: ResourceKind`)
- Modify: `src/kdive/db/repositories.py` (RUNS row mapping for new column + nullable system_id)
- Test: `tests/db/test_migrate.py` (or the existing migration test module)

**Interfaces:**
- Produces: `runs.target_kind text NOT NULL`; `runs.system_id` nullable; `Run.target_kind`
  (`ResourceKind`), `Run.system_id: UUID | None`.

- [ ] **Step 1: Write the migration SQL** exactly as in the spec's Data-model block (drop NOT
  NULL on `system_id`; add `target_kind text`; backfill via `systems→allocations→resources`
  join; `DO $$` guard raising if any `target_kind IS NULL` remains; `SET NOT NULL`). Header
  comment cites ADR-0169.

- [ ] **Step 2: Write the failing migration tests (two).**
  - *Backfill test* (the load-bearing one): apply migrations through `0041` only —
    `mig = migrate.discover_migrations(); apply each m with m.version <= "0041"` (run each
    `m.sql` under `pg_conn`, mirroring `apply_migrations`' execute) — then insert a
    Resource→Allocation→System→Run chain directly with raw SQL (the `0041` schema has no
    `target_kind` column, and `runs.system_id` is still `NOT NULL`). Apply `0042`. Assert the
    Run's `target_kind` now equals the backing resource's `kind`, and that the `DO $$` guard did
    not raise.
  - *Column-shape test*: after a full `apply_migrations(pg_conn)`, query
    `information_schema.columns` to assert `runs.target_kind` is `NOT NULL` and `runs.system_id`
    is nullable.
  - Reuse the existing list-twice idempotency pattern and `discover_migrations()` usage already
    in the migration test module.

- [ ] **Step 3: Run it — expect FAIL** (column `target_kind` absent / `system_id` still NOT NULL).
  `uv run python -m pytest tests/db/test_migrate.py -q` (set `KDIVE_REQUIRE_DOCKER=1`).

- [ ] **Step 4: Update the domain model + a bound-access helper.** In
  `domain/lifecycle/__init__.py`, change `system_id: UUID` → `system_id: UUID | None = None`,
  add `target_kind: ResourceKind` (import from `kdive.domain.catalog.resources`), and add a
  helper that keeps the tree type-checking immediately:

  ```python
  def require_system_id(self) -> UUID:
      """Return the bound System id, or fail closed for an unbound Run.

      Consumers that structurally require a bound System (install/boot/the system-join
      lookups) call this; the unbound lanes (build, create, bind) never do.
      """
      if self.system_id is None:
          raise CategorizedError(
              "run is not bound to a system",
              category=ErrorCategory.CONFIGURATION_ERROR,
              details={"run_id": str(self.id), "reason": "run_not_bound"},
          )
      return self.system_id
  ```

  Update `db/repositories.py` RUNS insert/select to read/write `target_kind` and tolerate NULL
  `system_id`.

- [ ] **Step 5: Route every existing bound consumer through the helper in THIS commit.**
  `git grep -n "\.system_id" src/kdive` and, at each site that currently assumes a bound System
  (`runs_install.py`, `runs_boot.py`, `cancel.py`, `view.py`, the resolver join callers), replace
  `run.system_id` with `run.require_system_id()` so `just type` passes now. Tasks 2 and 5 later
  replace these helper calls with real unbound handling where decoupling applies (the install/boot
  guard, the cancel tolerance); until then the helper preserves today's behavior and keeps the
  tree green. Run `just type` — **expected: passes** (zero errors). Run the migration tests —
  expected PASS.

- [ ] **Step 6: Commit.** `feat(runs): add nullable system_id + target_kind (migration 0042)`

> Note: do not paper over the Optional with `cast` or bare `assert`. The `require_system_id()`
> helper is the single, audited bound-access seam; every later guard either keeps it (genuinely
> bound paths) or replaces it with an unbound-aware branch (Tasks 2, 5).

---

### Task 2: Build resolves its builder from `target_kind`

**Files:**
- Modify: `src/kdive/providers/core/resolver.py` (use existing `resolve(kind)`; no new SQL needed)
- Modify: `src/kdive/jobs/handlers/runs_build.py:90-112` (`_run_build`: builder from
  `run.target_kind`)
- Test: `tests/jobs/handlers/test_runs_build.py`

**Interfaces:**
- Consumes: `Run.target_kind` (Task 1); `ProviderResolver.resolve(kind) -> ProviderRuntime`.
- Produces: `_run_build` no longer calls `resolver.runtime_for_run`; the build never touches the
  System.

- [ ] **Step 1: Write the failing test.** A build over an **unbound** Run (`system_id=None`,
  `target_kind=local-libvirt`) resolves the builder and produces a `BuildOutput`, asserting the
  resolver is consulted by kind, not by run→system join. Drive `_run_build`/`build_handler` with
  an injected fake resolver whose `resolve(LOCAL_LIBVIRT)` returns a stub runtime with a fake
  builder; assert the build succeeds with `system_id=None`.

- [ ] **Step 2: Run — expect FAIL** (current code calls `runtime_for_run`, which NOT_FOUNDs on a
  null system join).

- [ ] **Step 3: Implement.** In `_run_build`, replace
  `builder = (await resolver.runtime_for_run(conn, run_id)).builder` with
  `builder = resolver.resolve(run.target_kind).builder`. `run` is already a parameter.

- [ ] **Step 4: Run — expect PASS.** Also add/keep a bound-Run build regression test.

- [ ] **Step 5: Commit.** `feat(runs): resolve the build's builder from target_kind`

---

### Task 3: `runs.create` bound + unbound paths

**Files:**
- Modify: `src/kdive/services/runs/admission.py` (split paths; factor `_admit_system_for_run`;
  validate `target_kind`; inject `ProviderResolver`)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/create.py` (request dataclass: optional
  `system_id`, add `target_kind`)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py:86-136` (optional `system_id`, add
  `target_kind` param, pass resolver)
- Test: `tests/services/runs/test_admission.py`, `tests/mcp/test_runs_tools.py`

**Interfaces:**
- Consumes: `Run.target_kind` (Task 1); `ProviderResolver.registered_kinds()`,
  `runtime_for_system` (kind lookup), `resolve`.
- Produces: `create_run(pool, ctx, request, *, resolver)` now resolver-injected;
  `RunCreateResult` gains `target_kind: ResourceKind` and `system_id: UUID | None`;
  `_admit_system_for_run(conn, ctx, *, run_or_targets, system_id, requirement) ->` admission
  result reused by Task 4.

- [ ] **Step 1: Failing tests** for: (a) bound create stores `target_kind` = system's kind;
  (b) bound create with explicit matching `target_kind` ok; (c) bound create with mismatched
  explicit `target_kind` → `configuration_error` `target_kind_mismatch`; (d) unbound create
  success (`system_id=None`, kind stored, investigation `open→active`, next action
  `runs.build`); (e) unbound missing `target_kind` → `configuration_error` `target_kind_required`
  with `available_target_kinds`; (f) unbound unknown `target_kind` → `unknown_target_kind` with
  `available_target_kinds`; (g) unbound with `reuse_requirement` → `reuse_requires_system`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** In `admission.py`:
  - `RunCreateRequest.system_id: str | None`; add `target_kind: str | None`.
  - `create_run` gains `resolver: ProviderResolver`. Branch: `system_id is None` → unbound path;
    else bound path.
  - **Unbound path** `_create_unbound`: validate `target_kind` present (`target_kind_required`)
    and `ResourceKind(target_kind) in resolver.registered_kinds()` (`unknown_target_kind`); both
    errors put `available_target_kinds=sorted(k.value for k in resolver.registered_kinds())` in
    `data`. Reject `reuse_requirement` if non-empty (`reuse_requires_system`). Validate the
    investigation (reuse `INVESTIGATIONS.get` + project + `require_role` + open-for-run). Run the
    existing `_compat_block_response`. Under INVESTIGATION lock only, insert
    `Run(system_id=None, target_kind=…, state=CREATED)`, flip investigation, set `last_run_at`.
  - **Bound path**: keep `_resolve_targets`/`_create_locked`; after the System is fetched, derive
    `kind = await resolver.runtime_for_system`-style kind (extract the kind, not the runtime — add
    `ProviderResolver.kind_for_system(conn, system_id) -> ResourceKind` exposing the existing
    private `_kind`). If explicit `target_kind` given and `!= system_kind` →
    `target_kind_mismatch`. Store `target_kind=system_kind`.
  - Factor the System-admission block (`_preconditions_block_response` + `_assertion_block_response`
    under the ALLOCATION→SYSTEM→INVESTIGATION locks) into `_admit_system_for_run` so Task 4 reuses
    it. Keep functions ≤100 lines / complexity ≤8 — split helpers as needed.
  - `registrar.py`: `system_id: Annotated[str | None, …] = None`; add
    `target_kind: Annotated[str | None, Field(description="Resource kind to build for when no
    system_id is given; discover valid values from a runs.create error's available_target_kinds.
    Derived from the system when system_id is set.")] = None`. Thread the `resolver` into
    `_register_runs_create` (it already has `resolver` in `register`).

- [ ] **Step 4: Run — expect PASS;** then `just lint && just type`.

- [ ] **Step 5: Commit.** `feat(runs): runs.create bound/unbound paths with target_kind`

---

### Task 4: `runs.bind` tool + service

**Files:**
- Create: `src/kdive/services/runs/bind.py` (`bind_run`)
- Create: `src/kdive/mcp/tools/lifecycle/runs/bind.py` (tool handler `bind_run` wrapper)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (register `runs.bind`)
- Modify: `src/kdive/mcp/exposure.py` (`PUBLIC_TOOLS["runs.bind"] = _OPERATOR` — the same
  `ExposureScope.PROJECT_OPERATOR` constant `runs.create`/`runs.build` use, exposure.py:167-169),
  and `tests/mcp/test_tool_docs.py`
- Test: `tests/services/runs/test_bind.py`, `tests/adversarial/test_runs_bind_races.py`

**Interfaces:**
- Consumes: `_admit_system_for_run` (Task 3), `Run.target_kind`,
  `ProviderResolver.kind_for_system`.
- Produces: tool `runs.bind(run_id, system_id, reuse_requirement?)` → `ToolResponse` with
  `suggested_next_actions=["runs.install"]`.

- [ ] **Step 1: Failing tests:** success (sets `system_id`, audited, next action
  `runs.install`); kind mismatch → `configuration_error` `target_kind_mismatch`; already-bound →
  `transport_conflict` `run_already_bound`; terminal (`failed`/`canceled`) Run → `stale_handle`;
  one-Run-per-System (target has a live run) → `transport_conflict`; reuse assertion miss →
  `configuration_error`. Adversarial: two concurrent binds of one Run (CAS — exactly one wins,
  loser `run_already_bound`); two Runs racing for one System (one wins, other one-run-per-system).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `bind_run`.** Lock order PROJECT<RESOURCE<ALLOCATION<SYSTEM<INVESTIGATION
  <RUN — acquire ALLOCATION→SYSTEM→INVESTIGATION→RUN. Steps in fixed order (most-specific-first):
  run bindable (`system_id IS NULL`, state ∈ {created,running,succeeded}); `_admit_system_for_run`
  (ready system + live allocation + single project + one-run-per-system + optional reuse); kind
  match (`resolver.kind_for_system(system) == run.target_kind`). Then
  `UPDATE runs SET system_id=%s, updated_at=now() WHERE id=%s AND system_id IS NULL` — 0 rows →
  `transport_conflict` `run_already_bound`. Audit `runs.bind` transition. Register the tool
  (`mutating()`, `meta={"maturity":"implemented"}`, OPERATOR), add to `PUBLIC_TOOLS` and
  `test_tool_docs`.

- [ ] **Step 4: Run — expect PASS;** run `tests/adversarial/test_runs_bind_races.py`, `just lint`,
  `just type`.

- [ ] **Step 5: Commit.** `feat(runs): add runs.bind to attach a system to an unbound run`

---

### Task 5: install/boot unbound guards + cancel tolerance

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/steps.py` (`install_run`/`boot_run` MCP admission)
- Modify: `src/kdive/jobs/handlers/runs_install.py:39-52`, `runs_boot.py` (defensive guard)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/cancel.py` (NULL `system_id` tolerance)
- Test: `tests/mcp/test_runs_tools.py`, `tests/jobs/handlers/test_runs_install.py`,
  `tests/services/runs/test_cancel.py`

**Interfaces:**
- Consumes: `Run.system_id: UUID | None`.

- [ ] **Step 1: Failing tests:** `runs.install` / `runs.boot` on an unbound Run →
  `configuration_error` `run_not_bound`, `suggested_next_actions=["runs.bind"]` (MCP boundary, no
  job enqueued); worker `install_handler` with a null-system Run raises `configuration_error`;
  `runs.cancel` of an unbound `created` and `running` Run succeeds (no system dereference).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** In `steps.py` admission, before enqueueing: if `run.system_id is
  None` return `ToolResponse` failure `configuration_error` (`reason: run_not_bound`,
  `suggested_next_actions=["runs.bind"]`). In `runs_install.py`/`runs_boot.py`, add an explicit
  `if run.system_id is None: raise CategorizedError(..., CONFIGURATION_ERROR)` before the
  `SYSTEMS.get(run.system_id)` call. In `cancel.py`, guard any `system_id` use with a
  `None`-check (an unbound cancel frees nothing).

- [ ] **Step 4: Run — expect PASS;** `just lint && just type`.

- [ ] **Step 5: Commit.** `feat(runs): guard install/boot of an unbound run; cancel tolerates it`

---

### Task 6: Discovery — expose resource `kind` on system listings

**Files:**
- Modify: `src/kdive/mcp/tools/ops/inventory.py:150-159` (`_system_data` add `kind`; the list
  query must select the resource kind via the `systems→allocations→resources` join)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/view.py` (+ the `systems.list` query) to include
  `kind`
- Test: `tests/mcp/test_inventory_tools.py`, `tests/mcp/test_systems_tools.py`

**Interfaces:**
- Produces: `systems.list` / `inventory.list` system rows include `kind` (the resource kind).

- [ ] **Step 1: Failing test:** `inventory.list` and `systems.list` over a System return a `kind`
  field equal to the backing resource's kind.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Extend the system listing SQL to join the resource and select its
  `kind`; add `"kind": _as_str(row["kind"])` to `_system_data` and the systems view payload.

- [ ] **Step 4: Run — expect PASS;** `just lint && just type`.

- [ ] **Step 5: Commit.** `feat(systems): expose resource kind on system listings`

---

### Task 7: Next-action wiring, full suite, generated docs

**Files:**
- Modify: `create.py`/`server_build.py`/`bind.py` next-action constants as needed
- Modify: any generated tool-doc snapshot / `test_tool_docs` expectations
- Test: full `just ci`

- [ ] **Step 1:** Wire `suggested_next_actions`: unbound `create` → `["runs.build"]`; build
  success on an unbound Run → `["runs.bind"]` (bound → `["runs.install"]` unchanged); `bind` →
  `["runs.install"]`. Add/adjust tests asserting each.

- [ ] **Step 2:** Regenerate any committed tool-doc snapshots invalidated by the new tool /
  signature (`runs.bind`, `runs.create` params). Review the diff.

- [ ] **Step 3:** Run the **full** suite: `just ci`. Fix every failure (boundary/arch/doc tests
  live outside touched dirs — `test_tool_docs`, exposure, migrate-list-twice).

- [ ] **Step 4: Commit.** `test(runs): wire next-actions; regen tool docs for runs.bind`

---

## Self-Review

**Spec coverage:** migration 0042 + guard (T1) ✓; nullable `system_id`/`target_kind` (T1) ✓;
builder-from-`target_kind` (T2) ✓; bound/unbound `create` + self-correcting errors (T3) ✓;
`runs.bind` factored admission + CAS + races (T4) ✓; install/boot guards + cancel (T5) ✓;
discovery `kind` (T6) ✓; next-actions + full suite + doc regen (T7) ✓; unbound-Run lifecycle is a
no-code-change spec statement (no reaper added) — verified by T5 cancel tests. ✓

**Placeholders:** none — each task names exact files, the error reasons come from the spec table,
and test intent is concrete. Where exact existing-line code is shown as a range, the implementer
reads that file first (these tasks run inline with full context).

**Type consistency:** `target_kind: ResourceKind` on the domain model and result;
`system_id: UUID | None`; `_admit_system_for_run` shared by T3/T4; `kind_for_system` added to the
resolver in T3 and consumed in T4. Names consistent across tasks.
