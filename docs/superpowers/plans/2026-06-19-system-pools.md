# System Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent allocate the first-available system from a named pool of interchangeable resources, and let the remote-libvirt provider serve more than one host (de-singletoning) so remote pools work.

**Architecture:** Part A adds `pool` as a third allocation-request selection axis (a label on `resources`, reusing the existing first-available candidate resolution and FIFO promotion sweep). Part B threads the granted resource identity through the per-op remote-libvirt path by binding the connection config to the resource at the single `ProviderResolver` chokepoint, then removes the singleton guards. See spec `docs/superpowers/specs/2026-06-19-system-pools-design.md`, ADR-0186 (pool axis), ADR-0187 (de-singletoning).

**Tech Stack:** Python 3.14, `uv`, `psycopg` (async), pydantic, FastMCP, Postgres. Tests with `pytest`.

## Global Constraints

- Run guardrails before every commit: `just lint`, `just type` (whole tree, src + tests), and the focused tests for the files you touched. `just type` must be clean (strict `ty`, no project-wide relaxations).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- Absolute imports only (`from kdive.x import y`), never relative.
- Every tool returns a `ToolResponse` (`mcp/responses.py`); a failure status carries the most specific `ErrorCategory` (`domain/errors.py`) — never invent strings.
- ADRs: this work implements ADR-0186 and ADR-0187 (already written + Accepted). Cite them in new/changed module docstrings where the surrounding code cites ADRs.
- Migrations are forward-only, numbered monotonically; the next free number is **0045**. Migration version-list test assertions must be updated (there are several; `rg -n "0044" tests/` to find them).
- Commit messages: Conventional Commits, imperative subject ≤72 chars, end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- TDD: failing test first, confirm it fails for the expected reason, minimal implementation, confirm green, refactor green.
- Do **not** weaken test gating (`live_vm` / `live_stack` markers stay gated).

---

## Part A — Pool selection axis (ADR-0186)

### Task A1: Add `requested_pool` column (migration 0045)

**Files:**
- Create: `src/kdive/db/schema/0045_allocation_requested_pool.sql`
- Test: `tests/db/test_migrate.py` (existing — update version-list assertions)

**Interfaces:**
- Produces: a nullable `allocations.requested_pool text` column; queued pool rows persist it.

- [ ] **Step 1: Write the failing test.** In `tests/db/test_migrate.py`, find the assertion listing migration versions (search `0044`). Add `"0045"` to the expected list (and any "latest version" constant). Find the schema-shape test that asserts `allocations` columns (search `requested_kind` in `tests/db/`); add a case asserting `requested_pool` exists and is nullable `text`.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/db/test_migrate.py -q`
  Expected: FAIL — `0045` migration file missing / column absent.

- [ ] **Step 3: Write the migration.** Mirror migration 0016's `requested_kind` addition:

```sql
-- 0045_allocation_requested_pool.sql — pool selection axis (ADR-0186, #561). Additive,
-- forward-only (ADR-0015). A queued by-pool allocations.request persists its target pool here
-- so the FIFO promotion sweep can re-resolve candidates; mirrors requested_kind (0016). NULL for
-- by-id / by-kind requests. The "exactly one target selector" invariant among
-- requested_resource_id / requested_kind / requested_pool is enforced in the service layer
-- (as 0016 did for requested_kind), not a SQL XOR CHECK.
ALTER TABLE allocations
    ADD COLUMN requested_pool text;
```

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/db/test_migrate.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/db/schema/0045_allocation_requested_pool.sql tests/db/test_migrate.py
git commit -m "feat(db): add allocations.requested_pool for pool selection (#561)"
```

### Task A2: Carry `requested_pool` through the Allocation model + persistence

**Files:**
- Modify: `src/kdive/domain/lifecycle/__init__.py` (Allocation, ~line 59-61)
- Modify: `src/kdive/services/allocation/admission/core.py` (`AllocationRequest` ~line 99-131; the INSERT ~line 598-600; the row→Allocation read)
- Test: `tests/services/allocation/test_admission.py` (or the file holding admission persistence tests — `rg -l "requested_kind" tests/`)

**Interfaces:**
- Consumes: the `requested_pool` column (A1).
- Produces: `Allocation.requested_pool: str | None`; `AllocationRequest.requested_pool: str | None = None`; persisted at enqueue and read back.

- [ ] **Step 1: Write the failing test.** In the admission test module, add a test that admits a by-pool request with `on_capacity=queue` against an all-busy pool and asserts the persisted `Allocation.requested_pool == "<pool>"` and `requested_kind is None`. (Use the existing by-kind queue test as the template; `rg -n "requested_kind" tests/` to find it.)

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/services/allocation/ -q -k pool`
  Expected: FAIL — `requested_pool` unknown / not persisted.

- [ ] **Step 3: Implement.**
  - `domain/lifecycle/__init__.py`: add to `Allocation` next to line 60-61: `requested_pool: str | None = None`.
  - `core.py` `AllocationRequest`: add `requested_pool: str | None = None` next to `requested_kind` (line 130).
  - `core.py` the enqueue INSERT (~line 598-600): add `requested_pool=request.requested_pool` to the persisted columns (add `requested_pool` to the SQL column list + params).
  - Wherever a row is read into `Allocation` (search `requested_kind=row` / `model_validate` for allocations), include `requested_pool`.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/services/allocation/ -q -k pool`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/domain/lifecycle/__init__.py src/kdive/services/allocation/admission/core.py tests/
git commit -m "feat(allocation): persist requested_pool on queued allocations (#561)"
```

### Task A3: Pool branch in placement candidate resolution

**Files:**
- Modify: `src/kdive/services/allocation/admission/placement.py` (`PlacementRequest` line 19-24; `_schedulable_candidates` line 63-90)
- Test: `tests/services/allocation/test_placement.py` (`rg -l "_schedulable_candidates\|resolve_placement_candidates" tests/`)

**Interfaces:**
- Consumes: `resources.pool` column.
- Produces: `PlacementRequest(pool: str | None = None)`; `_schedulable_candidates` resolves a pool candidate set ordered `created_at, id`, affinity-filtered.

- [ ] **Step 1: Write the failing test.** Add tests: (a) two available resources with `pool='big'` are returned oldest-first for `PlacementRequest(pool='big')`; (b) a cordoned/offline `pool='big'` member is excluded; (c) an affinity-disallowed scoped `pool='big'` member is excluded when `project` is set; (d) `PlacementRequest(pool='nope')` → empty list. Insert resources with the test's existing resource-insert helper, setting `pool`.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/services/allocation/test_placement.py -q`
  Expected: FAIL — `PlacementRequest` has no `pool`.

- [ ] **Step 3: Implement.**
  - `PlacementRequest`: add `pool: str | None = None` (after `kind`).
  - `_schedulable_candidates`: change signature to also accept `pool: str | None`; after the `resource_id` branch and before the `kind is None` guard, add:

```python
    if pool is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM resources WHERE pool = %s AND status = 'available' "
                "AND NOT cordoned ORDER BY created_at, id",
                (pool,),
            )
            rows = await cur.fetchall()
        candidates = [Resource.model_validate(row) for row in rows]
        return [c for c in candidates if _affinity_ok(c, project)]
```

  - Update `resolve_placement_candidates` to pass `request.pool` into `_schedulable_candidates`.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/services/allocation/test_placement.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/services/allocation/admission/placement.py tests/
git commit -m "feat(allocation): resolve placement candidates by pool (#561)"
```

### Task A4: `ResourceByPool` payload variant

**Files:**
- Modify: `src/kdive/mcp/tool_payloads.py` (around `ResourceById`/`ResourceByKind` line 40-71; the `ResourceSelector` union + discriminator)
- Test: `tests/mcp/test_tool_payloads.py` (`rg -l "ResourceByKind\|ResourceById" tests/`)

**Interfaces:**
- Produces: `class ResourceByPool(ToolPayload)` with `mode: Literal["pool"]` and `pool: str`; added to the `ResourceSelector` discriminated union (`mode` discriminator).

- [ ] **Step 1: Write the failing test.** Assert that `AllocationRequestPayload.model_validate({"resource": {"mode": "pool", "pool": "big"}, ...})` parses and `payload.resource.pool == "big"`; and that supplying two selector keys still fails (the union enforces one).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/mcp/test_tool_payloads.py -q`
  Expected: FAIL — unknown discriminator `pool`.

- [ ] **Step 3: Implement.** Find the existing `mode` discriminator literals on `ResourceById`/`ResourceByKind` (e.g. `mode: Literal["id"]` / `Literal["kind"]`). Add:

```python
class ResourceByPool(ToolPayload):
    mode: Literal["pool"] = "pool"
    pool: str = Field(min_length=1)
```

  Add `ResourceByPool` to the `ResourceSelector` union type so the `discriminator="mode"` Field accepts it.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/mcp/test_tool_payloads.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/mcp/tool_payloads.py tests/
git commit -m "feat(mcp): add ResourceByPool allocation-request selector (#561)"
```

### Task A5: Make `AdmissionRequestSpec` selector tri-state + selector-aware request flow

**Files:**
- Modify: `src/kdive/services/allocation/admission/request.py` (`AdmissionRequestSpec` line 30-42; `request_admission` line 62-116; `_select_target` line 141-155; `RequestAdmissionResult` line 45-59)
- Modify: `src/kdive/mcp/tools/lifecycle/allocations/request.py` (`_spec_from_payload` line 38-57)
- Test: `tests/services/allocation/test_admission.py`, `tests/mcp/test_allocations_tools.py`

**Interfaces:**
- Consumes: `ResourceByPool` (A4), `PlacementRequest.pool` (A3), `AllocationRequest.requested_pool` (A2).
- Produces: `AdmissionRequestSpec(kind: ResourceKind | None, pool: str | None)`; a by-pool request that finds no pool member → `CONFIGURATION_ERROR` with a generic detail (no pool enumeration).

- [ ] **Step 1: Write the failing test.** Add service tests: (a) a by-pool request to an empty pool returns `CONFIGURATION_ERROR` whose detail does **not** contain any other pool's name; (b) a by-pool request to a pool with a free member grants and stamps `resource_id`; (c) the granted/queued allocation's `requested_pool` is set and `requested_kind` is None. Add a handler test that `allocations.request` with `{"mode":"pool","pool":"big"}` round-trips.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/services/allocation/test_admission.py tests/mcp/test_allocations_tools.py -q -k pool`
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `AdmissionRequestSpec`: change `kind: ResourceKind` → `kind: ResourceKind | None`; add `pool: str | None`.
  - `_spec_from_payload` (handler): branch on the three payload variants — `ResourceById` → `resource_id`; `ResourceByPool` → `pool=payload.resource.pool`, `kind=None`; `ResourceByKind` → `kind=payload.resource.kind`. Do not default `kind` when pool/id is set.
  - `request_admission`:
    - line 71 object_id: `object_id = (str(spec.resource_id) if spec.resource_id is not None else spec.pool if spec.pool is not None else spec.kind.value if spec.kind is not None else "<unspecified>")`. Extract a small `_selector_object_id(spec)` helper to keep complexity ≤8.
    - `_select_target`: pass `pool=spec.pool` into `PlacementRequest`.
    - line 88 available_kinds: set it only for a **by-kind** denial (`spec.kind is not None and spec.resource_id is None and spec.pool is None`). For a by-pool denial leave `available_kinds=None`.
    - line 108: `requested_kind=spec.kind if (spec.resource_id is None and spec.pool is None) else None`; add `requested_pool=spec.pool` to the `AllocationRequest`.
  - `_no_resource_response` (handler): when the object id came from a pool (track via a `RequestAdmissionResult` flag or detect `available_kinds is None and not a UUID`), return `f"no schedulable resource in pool {result.object_id!r} is registered"`. Keep by-id / by-kind details unchanged. (Cleanest: add `RequestAdmissionResult.selector: Literal["id","kind","pool"]` and switch on it.)

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/services/allocation/test_admission.py tests/mcp/test_allocations_tools.py -q`
  Expected: PASS (whole files, not just `-k pool`, to catch regressions in by-id/by-kind).

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/services/allocation/admission/request.py src/kdive/mcp/tools/lifecycle/allocations/request.py tests/
git commit -m "feat(allocation): admit by-pool requests, generic pool denial (#561)"
```

### Task A6: Promotion sweep re-resolves by `requested_pool`

**Files:**
- Modify: `src/kdive/services/allocation/promotion.py` (`_candidate_hosts` line 285-324; `_request_from_queued` line 327+)
- Test: `tests/services/allocation/test_promotion.py` + `tests/adversarial/` (promotion race)

**Interfaces:**
- Consumes: `Allocation.requested_pool` (A2), `PlacementRequest.pool` (A3).
- Produces: a queued by-pool allocation is promoted to the first freed pool member.

- [ ] **Step 1: Write the failing test.** In the promotion test: enqueue a by-pool allocation (busy pool), free a member, run `promote_pending`, assert the allocation is `GRANTED` on the freed member. In `tests/adversarial/`, extend the existing promotion race to two queued by-pool requests racing one freed slot (assert no double-grant).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/services/allocation/test_promotion.py -q -k pool`
  Expected: FAIL — pool not threaded into re-resolution.

- [ ] **Step 3: Implement.** In `_candidate_hosts`, add `pool=alloc.requested_pool` to **both** `PlacementRequest(...)` constructions (the PCIe-error fallback ~line 299 and the main path ~line 317). Confirm `_request_from_queued` carries `requested_pool` forward into the re-admission `AllocationRequest` (mirror its `requested_kind` handling).

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/services/allocation/test_promotion.py tests/adversarial/ -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/services/allocation/promotion.py tests/
git commit -m "feat(allocation): promote queued by-pool requests to freed members (#561)"
```

### Task A7: Declare pools — inventory `pool` field + reconcile write

**Files:**
- Modify: `src/kdive/inventory/model.py` (`_Instance` line 83-88 — add `pool`)
- Modify: `src/kdive/inventory/reconcile_resources.py` (`_insert_config_resource` line 265-284, `_update_config_resource` line 312-331, `_overlay_one_local` line 379-408, the `_DeclaredResource` dataclass line 117-124)
- Test: `tests/inventory/test_reconcile_resources.py`

**Interfaces:**
- Consumes: `resources.pool` column.
- Produces: a `[[remote_libvirt]]`/`[[fault_inject]]`/`[[local_libvirt]]` instance may declare `pool = "..."`; reconcile writes it (absent → `'default'`).

- [ ] **Step 1: Write the failing test.** Add: an instance with `pool="big"` produces a resource row with `pool='big'`; an instance without `pool` produces `pool='default'`; changing an instance's pool overlays the existing row's `pool`.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/inventory/test_reconcile_resources.py -q -k pool`
  Expected: FAIL — `_Instance` has no `pool`.

- [ ] **Step 3: Implement.**
  - `_Instance`: add `pool: str = "default"`.
  - `_DeclaredResource`: add `pool: str`; populate it from `inst.pool` at the two construction sites (fault_inject ~line 167, remote_libvirt ~line 187).
  - `_insert_config_resource`: change the INSERT to write `pool` from `declared.pool` instead of the hardcoded `'default'` (replace `'default'` literal in the column list with a `%s` param).
  - `_update_config_resource`: include `pool` in the change-detect comparison and the UPDATE.
  - `_overlay_one_local`: thread `inst.pool` into the local overlay UPDATE + change-detect (local rows are discovery-created; the overlay sets name/cost — add pool the same way).

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/inventory/test_reconcile_resources.py -q`
  Expected: PASS (whole file).

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/inventory/model.py src/kdive/inventory/reconcile_resources.py tests/
git commit -m "feat(inventory): declare resource pool in systems.toml (#561)"
```

### Task A8: Echo `requested_pool` in allocations recovery context + docs regen

**Files:**
- Modify: the allocations `get`/`list` recovery-context builder (`rg -n "requested_kind\|requested_selector\|requested_resource_id" src/kdive/mcp/tools/lifecycle/allocations/`)
- Modify: `systems.toml.example` (document the optional `pool` field)
- Test: `tests/mcp/test_allocations_tools.py`
- Regen: generated tool docs (`just docs-check` target)

**Interfaces:**
- Consumes: `Allocation.requested_pool` (A2).
- Produces: `allocations.get`/`list` echo `requested_pool` when set.

- [ ] **Step 1: Write the failing test.** Assert a queued by-pool allocation's `allocations.get` `data` includes `requested_pool`.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/mcp/test_allocations_tools.py -q -k pool`
  Expected: FAIL.

- [ ] **Step 3: Implement.** Add `requested_pool` to the recovery-context echo next to where `requested_kind`/the selector is echoed (ADR-0180 pattern). Add a commented `pool = "big-remote"` example under a `[[remote_libvirt]]` block in `systems.toml.example`.

- [ ] **Step 4: Run to verify it passes + regen docs.**
  Run: `uv run python -m pytest tests/mcp/test_allocations_tools.py -q`
  Then regen generated docs: run the generator the way `just docs-check` diffs (find it: `rg -n "gen_tool_reference\|docs-check" justfile`), regenerate, and `just docs-check`.
  Run: `just docs-check && just config-docs-check`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add -A
git commit -m "feat(allocation): surface requested_pool; document pool field (#561)"
```

---

## Part B — Remote-libvirt de-singletoning (ADR-0187, #395)

### Task B1: Add `remote_config_for_resource` + `all_remote_configs` (keep the singleton until B8)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/config.py` (factor out `_build_config`; add the two new functions; **keep** `remote_config_from_inventory` / `_resolve_instance` / `_require_single_instance` untouched for now)
- Test: `tests/providers/remote_libvirt/test_config.py`

**Interfaces:**
- Produces:
  - `remote_config_for_resource(resource_name: str) -> RemoteLibvirtConfig` — selects the instance whose `name == resource_name`; zero matches → `CONFIGURATION_ERROR` naming the missing instance.
  - `all_remote_configs() -> list[RemoteLibvirtConfig]` — validates and returns every declared instance.
  - `resolve_base_image_staged_volume_for(resource_name: str) -> str` — by-name variant (the old no-arg `resolve_base_image_staged_volume` stays until its callers migrate in B6).
- **Green-at-every-commit:** B1 is purely additive. `remote_config_from_inventory` and the singleton guards are **not** deleted here — they are deleted in **B8** only after B2–B7 migrate every caller. This keeps `just type`/`just test` green at each commit (deleting the symbol now would break ~15 importers).

- [ ] **Step 1: Write the failing test.** With two `[[remote_libvirt]]` instances (`a`, `b`) in a temp `systems.toml`: `remote_config_for_resource("b").uri` is b's URI; `remote_config_for_resource("c")` raises `CONFIGURATION_ERROR`; `all_remote_configs()` returns 2 configs; per-instance validation (`validate_remote_uri`, gdbstub range) still fires for the selected instance. (Note: today's parser still rejects 2 instances — Task B4 relaxes it. For this test, construct the instances list directly / monkeypatch `_load_remote_instances` to return two, rather than parsing a 2-instance file, so B1 does not depend on B4.)

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q`
  Expected: FAIL — new functions absent.

- [ ] **Step 3: Implement.** Add a private `_build_config(instance) -> RemoteLibvirtConfig` factoring out the body of today's `remote_config_from_inventory` (validate URI, parse range, construct), and have `remote_config_from_inventory` call it (so behavior is unchanged). Then add:

```python
def remote_config_for_resource(resource_name: str) -> RemoteLibvirtConfig:
    instances = _load_remote_instances()
    instance = next((i for i in instances if i.name == resource_name), None)
    if instance is None:
        names = sorted(i.name for i in instances)
        raise CategorizedError(
            f"no [[remote_libvirt]] instance named {resource_name!r} is declared in "
            f"systems.toml (declared: {names})",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return _build_config(instance)


def all_remote_configs() -> list[RemoteLibvirtConfig]:
    return [_build_config(i) for i in _load_remote_instances()]
```

  Add a `resolve_base_image_staged_volume_for(resource_name)` by-name variant alongside the existing function (do not remove the old one yet).

- [ ] **Step 4: Run to verify it passes.**
  Run: `just type && uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q`
  Expected: PASS — tree stays green (singleton still present, all callers compile).

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/config.py tests/
git commit -m "feat(remote): add by-name + fleet remote config resolvers (#395)"
```

### Task B2: `ProviderRuntime.for_resource` rebind hook + remote build_runtime parameterization

**Files:**
- Modify: `src/kdive/providers/core/runtime.py` (`ProviderRuntime` dataclass — add `for_resource`)
- Modify: `src/kdive/providers/remote_libvirt/composition.py` (`build_runtime` line 188-232; thread `config_factory` into the class-(a) lifecycle ports; set `rebind_for_resource`)
- Modify: the class-(a) lifecycle port constructors / `from_env` so `build_runtime` can pass a bound `config_factory` (do NOT remove their `= remote_config_from_inventory` default): `lifecycle/{provisioning,connect,install,control,build_vm}.py`, `debug/introspect.py`, `retrieve/facade.py`, `staged_volumes.py`. The class-(b)/(c) modules are migrated in B5/B6, not here.
- Test: `tests/providers/core/test_runtime.py`, `tests/providers/remote_libvirt/test_composition.py`

**Interfaces:**
- Consumes: `remote_config_for_resource` (B1).
- Produces: `ProviderRuntime.for_resource(resource_name: str) -> ProviderRuntime` (default returns `self`; remote rebuilds its ports with `config_factory=lambda: remote_config_for_resource(resource_name)`).

- [ ] **Step 1: Write the failing test.** (a) `runtime.for_resource("x")` on a local runtime returns an equivalent runtime (default no-op). (b) For remote, build the runtime with a stub `config_factory` and assert `for_resource("b")` produces ports whose resolved config is b's (inject a fake resolver to avoid touching `systems.toml`).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/core/test_runtime.py tests/providers/remote_libvirt/test_composition.py -q`
  Expected: FAIL — `for_resource` absent.

- [ ] **Step 3: Implement.**
  - `runtime.py`: add field `for_resource: Callable[[str], "ProviderRuntime"] = lambda self_runtime: ...`. Since a frozen dataclass can't easily reference `self` in a default, implement it as a **method** that reads an optional stored `_rebind: Callable[[str], ProviderRuntime] | None = None`:

```python
    rebind_for_resource: Callable[[str], "ProviderRuntime"] | None = None

    def for_resource(self, resource_name: str) -> "ProviderRuntime":
        """Return a runtime bound to ``resource_name``; default identity (no per-resource config)."""
        if self.rebind_for_resource is None:
            return self
        return self.rebind_for_resource(resource_name)
```

  - `remote_composition.build_runtime`: accept `config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_inventory` (keep the singleton as the unbound default so an unbound runtime still works exactly as today — it is deleted only in B8). Thread `config_factory` into every **class-(a) lifecycle port** `build_runtime` constructs. Set `rebind_for_resource=lambda name: build_runtime(secret_registry=secret_registry, config_factory=lambda: remote_config_for_resource(name))`.
  - **Green-at-every-commit (do NOT remove any default):** keep `config_factory: ... = remote_config_from_inventory` on **every** module's constructor/`from_env`. B2 only *adds* the ability for `build_runtime` to pass an override; it removes no default. The class-(a) lifecycle ports (`lifecycle/{provisioning,connect,install,control,build_vm}.py`, `debug/introspect.py`, `retrieve/facade.py`, `staged_volumes.py`) now receive the bound factory from `build_runtime`. The class-(b)/(c) modules (`transport_reset.py`, `reaping/connections.py`, `diagnostics/{reachability,base_image_staging,contribution}.py`) are **not touched in B2** — they keep the singleton default and are migrated together with their callers in B5/B6 (so no caller ever loses a required arg mid-flight). B8 removes the now-unused singleton and any default still referencing it.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/providers/core/ tests/providers/remote_libvirt/ -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/core/runtime.py src/kdive/providers/remote_libvirt/ tests/
git commit -m "feat(remote): bind remote runtime config per resource (#395)"
```

### Task B3: Resolver binds the per-System resource at the chokepoint

**Files:**
- Modify: `src/kdive/providers/core/resolver.py` (the `_KIND_FOR_*` SQL line 28-53 add `r.name`; `_kind` → `_kind_and_name` line 155-167; `runtime_for_system`/`_run`/`_allocation`/`_session` line 136-153 call `.for_resource(name)`)
- Test: `tests/providers/core/test_resolver.py`

**Interfaces:**
- Consumes: `ProviderRuntime.for_resource` (B2).
- Produces: `runtime_for_system`/`runtime_for_run`/`runtime_for_allocation`/`runtime_for_session` return a runtime bound to the object's resource name.

- [ ] **Step 1: Write the failing test.** Register a fake remote runtime whose `for_resource` records the name. Insert a System on a resource named `"b"`; assert `runtime_for_system(conn, sid)` invoked `for_resource("b")`. Assert a local System (no `rebind_for_resource`) returns the same runtime.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/core/test_resolver.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.** Add `r.name AS name` (or `res.name`) to each `_KIND_FOR_*` SELECT. Rename `_kind` → `_kind_and_name` returning `(ResourceKind, str)`. Each `runtime_for_*` resolves `(kind, name)` then `return self.resolve(kind).for_resource(name)`. Keep `binding_for_session` returning the bound runtime in `ProviderBinding`.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/providers/core/test_resolver.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/core/resolver.py tests/
git commit -m "feat(provider): bind runtime to the object's resource name (#395)"
```

### Task B4: Relax the inventory singleton guard

**Files:**
- Modify: `src/kdive/inventory/model.py` (`_check_remote_libvirt_singleton` line 229-238; `parse` line 282 call; the per-kind uniqueness check line 220-227 should already cover remote — confirm `remote_libvirt` names are in the uniqueness group)
- Test: `tests/inventory/test_model.py`

**Interfaces:**
- Produces: `InventoryDoc.parse` accepts N `[[remote_libvirt]]` instances (names unique); the singleton guard is gone.

- [ ] **Step 1: Write the failing test.** Two `[[remote_libvirt]]` blocks with distinct names parse successfully; two with the **same** name raise `InventoryError` (duplicate-name). Update/replace the existing test that asserts the singleton rejection (search `not supported until per-op`).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/inventory/test_model.py -q`
  Expected: FAIL — multi-instance still rejected.

- [ ] **Step 3: Implement.** Delete `_check_remote_libvirt_singleton` and its call at line 282. Ensure `_check_instance_name_uniqueness` (line ~218-227) includes `("remote_libvirt", [i.name for i in self.remote_libvirt])` in its `groups` tuple so duplicate remote names are still rejected.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/inventory/test_model.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/inventory/model.py tests/
git commit -m "feat(inventory): allow multiple remote-libvirt instances (#395)"
```

### Task B5: Console hosting + reconciler reset/reap resolve per resource (spec B.4/B.5 b)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/composition.py` (`build_console_hosting` line 130-170 — per-system config in `factory`)
- Modify: `src/kdive/providers/remote_libvirt/transport_reset.py` + its reconciler caller — resolve config per domain→resource
- Modify: `src/kdive/providers/remote_libvirt/reaping/connections.py` + caller — per-domain resolution, or `all_remote_configs()` fan-out when domain-less
- Test: `tests/providers/remote_libvirt/test_console_hosting.py`, `test_transport_reset.py`, `tests/reconciler/` reaper tests

**Interfaces:**
- Consumes: `remote_config_for_resource` (B1), `all_remote_configs` (B1), the reconciler's domain→System→Resource mapping.

- [ ] **Step 1: Write the failing test.** (a) Console factory for a system bound to resource `"b"` opens b's console (inject a fake `remote_config_for_resource`). (b) `RemoteLibvirtTransportResetter.reset(...)` for a domain on host `"b"` connects to b's URI. (c) A domain-less reaper sweep iterates all hosts (`all_remote_configs`).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/test_console_hosting.py tests/providers/remote_libvirt/test_transport_reset.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `build_console_hosting`: inside `factory(system_id)`, resolve the system's bound resource name (the running-systems source already carries enough to map system→resource; if not, pass a `resource_name_for(system_id)` resolver into the loop) and call `remote_config_for_resource(name)` for that console's `open_remote_console`. Keep leader lock / event loop / host pool bootstrap-resolved.
  - `transport_reset.py` / its reconciler caller: thread the resource name. The reset is invoked from the reconciler (`rg -n "TransportResetter\|\.reset(" src/kdive/reconciler`); the caller has the System/domain → resolve the resource name there and pass a name-bound `config_factory` (or the config) into the resetter.
  - `reaping/connections.py`: change `config_factory=remote_config_from_inventory` to a per-domain resolution where the caller has a domain→resource, else fan out over `all_remote_configs()` (find the reaper caller in `src/kdive/reconciler` / `providers/infra/reaping`).

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/ tests/reconciler/ -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/ src/kdive/reconciler/ tests/
git commit -m "feat(remote): per-resource console + reconciler reset/reap (#395)"
```

### Task B6: Diagnostics fan out per declared instance (spec B.5 c)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/diagnostics/{reachability,base_image_staging,contribution}.py` and the `gdbstub_acl` probe; their doctor caller
- Test: `tests/providers/remote_libvirt/diagnostics/` (or wherever the doctor probe tests live — `rg -l "reachability\|base_image_staging" tests/`)

**Interfaces:**
- Consumes: `all_remote_configs` (B1), `resolve_base_image_staged_volume_for(resource_name)` (B1).
- Produces: each remote diagnostic emits one result row per declared instance.

- [ ] **Step 1: Write the failing test.** With two instances, the reachability/base-image/contribution probe returns two result rows (one per host), not one.

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/ -q -k "diagnostic or reachability or staging"`
  Expected: FAIL.

- [ ] **Step 3: Implement.** Change each probe to iterate `all_remote_configs()` (and `resolve_base_image_staged_volume_for(name)` per instance), emitting per-host results. `contribution.py:50` `remote_config_from_inventory()` → loop over `all_remote_configs()`. Preserve each probe's existing per-host result shape.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/ -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/diagnostics/ tests/
git commit -m "feat(remote): fan out remote diagnostics per host (#395)"
```

### Task B7: Build-VM / ephemeral-build path resolves config by build-host resource name

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py` (line 229 `config_factory=remote_config_from_inventory`) and `build_ephemeral_build_transport_factory` (composition.py line 106-127) + the build-host dispatch caller
- Test: `tests/providers/remote_libvirt/test_build_vm.py`

**Interfaces:**
- Consumes: `remote_config_for_resource` (B1); `BuildHost` carries a resource name.

- [ ] **Step 1: Write the failing test.** A build-VM op for a build host named `"b"` resolves b's config (inject a fake resolver).

- [ ] **Step 2: Run to verify it fails.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/test_build_vm.py -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.** Thread the `BuildHost.name` (resource name) into the build-VM transport factory and `build_vm.py`'s `config_factory` as `lambda: remote_config_for_resource(host.name)`. Confirm `BuildHost` carries the resource name (`rg -n "class BuildHost" src/kdive`); if it carries a different identity, map it to the resource name at the dispatch site.

- [ ] **Step 4: Run to verify it passes.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/test_build_vm.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/ tests/
git commit -m "feat(remote): resolve build-VM config by build-host name (#395)"
```

### Task B8: Delete the singleton; sweep for residual references + full guardrails

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/config.py` (now-unused `remote_config_from_inventory`, `_resolve_instance`, `_require_single_instance`, the old no-arg `resolve_base_image_staged_volume`)
- Verify across `src/kdive` and `tests`.

- [ ] **Step 1: Confirm no production caller remains, then delete.**
  Run: `rg -n "remote_config_from_inventory|_require_single_instance|_resolve_instance|resolve_base_image_staged_volume\b" src/kdive`
  Expected: only the definitions in `config.py` remain (B2–B7 migrated every caller). If any other module still references them, that module's task (B2/B5/B6/B7) was incomplete — finish it first, do not delete. Then delete the four now-unused definitions from `config.py`.

- [ ] **Step 2: Grep proof + full local suite.**
  Run: `rg -n "remote_config_from_inventory|_require_single_instance|_resolve_instance|_check_remote_libvirt_singleton" src/kdive`
  Expected: **no matches**.
  Run: `just lint && just type && just test`
  Expected: PASS. Fix any remaining import of the deleted symbols.

- [ ] **Step 3: Add a regression guard test.** A small test that imports `kdive.providers.remote_libvirt.config` and asserts `not hasattr(module, "remote_config_from_inventory")` — so the singleton cannot silently return.

- [ ] **Step 4: Run the guard.**
  Run: `uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q`
  Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add tests/
git commit -m "test(remote): guard against singleton config resurrection (#395)"
```

---

## Self-review notes

- **Spec coverage:** A1-A8 cover Part A (migration, persistence, placement, payload, admission, promotion, declaration, observability). B1-B8 cover Part B (per-resource config, runtime rebind, resolver chokepoint, singleton relax, console/reconciler/diagnostics/build callers, residual sweep). The tenant-isolation safeguard is in A5 (generic pool denial, no enumeration) + its test.
- **Ordering:** A1→A2 (column before persistence); A3/A4 independent; A5 depends on A2/A3/A4; A6 on A2/A3; A7 independent; A8 on A2. B1 first (additive new API, keeps the singleton); B2 on B1; B3 on B2; B4 independent of B2/B3 but do after; B5/B6/B7 on B1/B2; **B8 last (deletes the singleton only after every caller migrated — every prior commit stays green)**. Run **sequentially on one branch, A then B**: Part A and Part B are **not** file-disjoint — A7 and B4 both edit `inventory/model.py` (A7 the `_Instance` model, B4 the singleton guard + parse), and A7 also edits `reconcile_resources.py`. Do not run them in parallel worktrees; serialize A7 before B4.
- **Risk — requested_pool XOR:** kept a service-layer invariant (A2/A5), no SQL XOR CHECK, matching 0016's `requested_kind` treatment (spec Open Risks).
- **Verify after base moves:** if `main` advances, re-run `just docs-check`/`config-docs-check` and the migration version-list test (A1).
