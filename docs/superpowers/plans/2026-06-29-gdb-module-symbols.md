# gdb Module-Symbol Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This change is tightly coupled (data model → resolver → engine list/load → ops/tools+wiring); execute the tasks in order in one session.

**Goal:** Add `debug.list_modules` and `debug.load_module_symbols` to the shared gdb-MI debug tier so an agent can enumerate loaded kernel modules and load a module's symbols over a live gdbstub `DebugSession`.

**Architecture:** Two ops on the shared `GdbMiEngine` (remote-libvirt inherits free) + two `contributor` MCP tools, mirroring ADR-0275/0276/0277. Enumeration walks the kernel `modules` list via internally-constructed `-data-evaluate-expression` casts (non-injectable). Loading re-reads the base fresh, verifies binary identity, resolves the `.ko` from the published `kernel_ref` tar via an injected resolver, and `add-symbol-file`s it. No schema/migration/RBAC/destructive-gate change.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`. gdb-MI via the existing `GdbController`/scripted-fake seam. See ADR-0278 and `docs/superpowers/specs/2026-06-29-issue-923-gdb-module-symbols-design.md`.

## Global Constraints

- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, 100-char lines, absolute imports only.
- Pick the most specific existing `ErrorCategory`; never invent strings. New `data["code"]` discriminators are fine.
- All textual MI/record output passes the `Redactor` before response (engine `_redactor()`); module names/identities are non-secret but still redacted like other record fields.
- Every gdb command is constructed from gated/numeric/engine-staged inputs — never caller text (the ADR-0034/0248/0277 non-injectability rule).
- **Every commit must be green:** `just lint` (`ruff check` + `ruff format --check`), `just type` (whole tree, src+tests), and the relevant `pytest` all pass before each commit. Full `just ci` before push. This drives the task grouping below: a Protocol method and its concrete + fault-inject implementations land in the **same** commit (the concrete `GdbMiEngine` flows into the Protocol type at `ops.py`, so a widened Protocol without impls fails `ty`); a tool registration and its `_TOOL_SCOPES`/`tool_index`/`_BEHAVIOR_TESTS_BY_TOOL`/generated-doc entries land in the same commit (completeness guards in `tests/mcp/core/test_tool_docs.py` fail otherwise).
- Tools marked `implemented` (`_gdbmi_maturity`), **not** added to `_LOCAL_PROVEN_DEBUG_TOOLS`.
- New ADR/spec already committed (ADR-0278). Do not renumber.

---

### Task 1: Data model — GdbModule + attachment fields (additive, green alone)

**Files:**
- Modify: `src/kdive/providers/ports/debug.py`

**Interfaces:**
- Produces: `GdbModule(ProviderModel)` with `name: str | None = None`, `base_address: str | None = None`, `symbols_loaded: bool | None = None`, `identity_verified: bool | None = None` (defaults so `model_dump(exclude_none=True)` drops list-only/load-only fields); `GdbModuleList(ProviderModel)` with `modules: list[GdbModule]`, `truncated: bool = False`, `decode_errors: int = 0` (the `list_modules` return type — a flat list cannot carry truncated/decode_errors); `GdbMiAttachment` gains `run_id: str = ""` and `loaded_modules: set[str] = field(default_factory=set)`.
- Note: the two Protocol *methods* are deliberately NOT added here — they land with their implementations in Tasks 3/4 so `just type` stays green.

- [ ] **Step 1:** Add the `GdbModule` and `GdbModuleList` models after `GdbWatchpointRef`; add `run_id`/`loaded_modules` to the `GdbMiAttachment` dataclass (defaults keep existing fakes/`engine.attach` construction working).
- [ ] **Step 2:** `just type` and `just lint` — Expected: PASS (purely additive; no Protocol widening, no impl gap).
- [ ] **Step 3:** Commit `feat(923): add GdbModule record + module attachment fields`.

---

### Task 2: ModuleDebuginfoResolver (.ko path + identity) + kernel_ref query

**Files:**
- Modify: `src/kdive/db/artifact_queries.py` (add `kernel_ref_for_run_sync`, paralleling `debuginfo_ref_for_run_sync:81`)
- Modify: `src/kdive/providers/shared/debug_common/debuginfo.py`
- Test: `tests/providers/shared/debug_common/test_debuginfo.py` (match the existing debuginfo test module path)

**Interfaces:**
- Produces: `ModuleDebuginfo(path: Path, srcversion: str | None, build_id: str | None)` (frozen dataclass); `ModuleDebuginfoResolver(read_kernel_ref, fetch_object, read_identity)` with `resolve(run_id: str, module: str) -> ModuleDebuginfo`; `kernel_ref_for_run_sync(conn, run_id: UUID) -> str | None`; a `real_module_debuginfo_resolver()` factory for the live seam.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write failing tests** (inject fakes — no real ELF/tar in the unit path): (a) `resolve` fetches+extracts a fixture tar (built in a tmp dir with `lib/modules/x/foo.ko`), locates `foo.ko`, and returns `ModuleDebuginfo` with identity from the injected `read_identity` fake → assert path + srcversion/build_id. (b) absent module/.ko → `CategorizedError(CONFIGURATION_ERROR, code="no_module_debuginfo")` with a remediation `data["reason"]`. (c) `-`/`_` variant: in-memory `foo_bar` matches file `foo-bar.ko`. (d) per-run caching: a second `resolve` for the same run_id does not re-fetch (assert fetch seam call count == 1).
- [ ] **Step 2:** Run `pytest tests/providers/shared/debug_common/test_debuginfo.py -k module -v` — Expected: FAIL (undefined).
- [ ] **Step 3: Implement.** Mirror `DebuginfoResolver`: inject `read_kernel_ref` (real = `kernel_ref_for_run_sync`), `fetch_object` (real = `default_fetch_object`), and `read_identity: Callable[[Path], tuple[str|None, str|None]]`. Stage the tar into a per-run cached `mkdtemp` dir, extract `lib/modules/`, glob for `<module>.ko` trying `module` and `module.replace("_","-")`. The real `read_identity` (`# pragma: no cover - live_vm`) reads the `.ko` ELF section headers for `.modinfo` (null-separated `key=value`, take `srcversion=`) and the `.note.gnu.build-id` note (hex-encode the descriptor) directly — no new dependency. `no_module_debuginfo` carries remediation naming `CONFIG_DEBUG_INFO=y` + module presence.
- [ ] **Step 4:** Run tests — Expected: PASS. `just lint` + `just type`.
- [ ] **Step 5:** Commit `feat(923): ModuleDebuginfoResolver for .ko path + identity`.

---

### Task 3: list_modules — Protocol + engine + fault-inject (one green commit)

**Files:**
- Modify: `src/kdive/providers/ports/debug.py` (add `list_modules` to the `GdbMiEngine` Protocol)
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py`
- Modify: `src/kdive/providers/fault_inject/debug/gdb.py`
- Test: the engine unit test module (`tests/.../test_gdbmi.py` or the scripted-controller tests under `tests/mcp/debug/` — match the tree) + the fault-inject debug test

**Interfaces:**
- Consumes: `GdbModule`, `GdbModuleList` (Task 1); `evaluate_value`, `execute_mi_command`, `_redactor`, `_config_error`, `_RUNNING_RE` (gdbmi.py).
- Produces: Protocol + concrete `list_modules(self, attachment, *, max_modules: int = MAX_MODULES) -> GdbModuleList`; module constant `MAX_MODULES = 512`; private helpers `_module_walk(self, attachment, *, limit) -> tuple[list[_RawModule], bool, int]` (returns rows with `name: str`/`base: int`/`node: int`, a `truncated` bool, and a `decode_errors` count) and `_module_base_field(self, attachment, first_node: int) -> str` (the one-time `mem[MOD_TEXT].base`→`core_layout.base` probe), both reused by Task 4. Fault-inject returns a deterministic synthetic `GdbModuleList`.
- **Base is `int`** end-to-end (`_module_walk` yields int; `list_modules` formats `0x{base:x}` into `GdbModule.base_address` and packs rows/truncated/decode_errors into `GdbModuleList`; Task 4 compares the int to `expected_base`).

- [ ] **Step 1: Write failing tests** against the scripted `MiController` fake: (a) two-module walk (head read `&modules`/`modules.next`, per-node `container_of`+name+base evals, terminator back to head) → two `GdbModule`s with hex `base_address`, `symbols_loaded=False`; assert the `mem[MOD_TEXT]` probe path. (b) bound: small `max_modules` → `truncated` semantics surfaced by the op (Task 5) — here assert the engine stops at the bound. (c) one garbage row → skipped, the other present (decode-error count exposed to Task 5). (d) running-target `^error` on head read → `inferior_running`. (e) both base-field probes `^error` → `module_decode_failed`. Plus a fault-inject test asserting Protocol conformance + deterministic output.
- [ ] **Step 2:** Run — Expected: FAIL (undefined).
- [ ] **Step 3: Implement** the Protocol method (returns `GdbModuleList`), the concrete walk (derive `<list-offset>` via `&((struct module *)0)->list`; loop bounded by `max_modules`; per-row `^error`/parse failure → skip+count; set `truncated` when a row exists past the bound; classify head/running errors), the base-field probe, and the fault-inject synthetic `list_modules`. `list_modules` redacts each row, formats `base_address`, and returns `GdbModuleList(modules=..., truncated=..., decode_errors=...)`. Keep functions ≤100 lines / complexity ≤8 via the two helpers.
- [ ] **Step 4:** Run engine + fault-inject tests — Expected: PASS. `just lint` + `just type` (Protocol now satisfied by both engines).
- [ ] **Step 5:** Commit `feat(923): list_modules kernel module-list walk (engine + fault-inject)`.

---

### Task 4: load_module_symbols — Protocol + engine + fault-inject (one green commit)

**Files:**
- Modify: `src/kdive/providers/ports/debug.py` (add `load_module_symbols` to the Protocol)
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py` (engine `__init__` gains `module_debuginfo_resolver` seam)
- Modify: `src/kdive/providers/fault_inject/debug/gdb.py`
- Modify: `src/kdive/providers/local_libvirt/composition.py:127` AND `src/kdive/providers/remote_libvirt/composition.py:260` — both construct the ops `engine=GdbMiEngine(...)`; pass `module_debuginfo_resolver=real_module_debuginfo_resolver()`. **Required**, not optional: the `__init__` default raises `MISSING_DEPENDENCY`, so an unwired resolver makes every live `load_module_symbols` fail. (Live-proof stays deferred, but the wiring must exist; the local/remote attach seams are already wired the same way.)
- Test: engine unit test module + fault-inject test

**Interfaces:**
- Consumes: Task 1 `GdbModule`; Task 2 `ModuleDebuginfoResolver`/`ModuleDebuginfo`; Task 3 `_module_walk`; `_mi_path`, `execute_mi_command` (gdbmi.py).
- Produces: Protocol + concrete `load_module_symbols(self, attachment, *, module: str, expected_base: int | None = None) -> GdbModule`; engine `__init__` param `module_debuginfo_resolver: Callable[[str, str], ModuleDebuginfo] | None = None` (default raises `MISSING_DEPENDENCY` like `attach`); `_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")`; private `_verify_identity(self, attachment, node, ko_identity) -> bool | None`.

- [ ] **Step 1: Write failing tests** (scripted controller + fake resolver): (a) success — fresh base read, fake resolver returns `.ko`+matching `srcversion`; assert `-interpreter-exec console "add-symbol-file <ko> 0x<base>"` issued, result `symbols_loaded=True identity_verified=True`, module in `attachment.loaded_modules`. (b) `bad_module_name` for `"foo;rm"` — no MI issued. (c) module absent from walk → `module_not_loaded`, no add-symbol-file. (d) `expected_base` ≠ current → `stale_module_address`, no add-symbol-file. (e) resolver raises `no_module_debuginfo` → propagates (CONFIGURATION_ERROR + remediation). (f) `mod->srcversion` ≠ resolver srcversion → `module_binary_mismatch`, no add-symbol-file. (g) both identity reads `^error` → loads, `identity_verified=False`; and build_id-fallback match (srcversion `^error`, build_id matches). (h) idempotent re-load (module already in `loaded_modules`, still present + non-stale) → returns `loaded`, no second add-symbol-file. Plus fault-inject conformance test.
- [ ] **Step 2:** Run — Expected: FAIL.
- [ ] **Step 3: Implement** in spec order: gate name → re-walk base (reuse `_module_walk`) + staleness (`module_not_loaded`/`stale_module_address`) → idempotency short-circuit (after staleness passes) → resolver → `_verify_identity` (read `mod->srcversion` then `mod->build_id`, catch `^error`; compare same-kind → `module_binary_mismatch`/True/None) → `-interpreter-exec console "add-symbol-file ..."` (wrap `^error` → `add_symbol_failed`) → record in `loaded_modules`. Update the gdbmi.py module docstring: drop "module loading remain outside this engine's contract" (now in-contract, ADR-0278). Add the fault-inject synthetic `load_module_symbols`.
- [ ] **Step 4:** Run — Expected: PASS. `just lint` + `just type`.
- [ ] **Step 5:** Commit `feat(923): load_module_symbols with identity verification (engine + fault-inject)`.

---

### Task 5: MCP tools + scopes + vocab + behavior-map + docs (one green commit)

**Files:**
- Modify: `src/kdive/mcp/tools/debug/ops.py` (two op factories + two registrations + `_register_debug_ops` count/docstring)
- Modify: `src/kdive/mcp/exposure.py` (`_TOOL_SCOPES` → both tools in `_CONTRIBUTOR`)
- Modify: `src/kdive/mcp/tool_index.py` (search vocabulary)
- Modify: the `_BEHAVIOR_TESTS_BY_TOOL` map (in `tests/mcp/core/test_tool_docs.py` per the grep) → both tools → the Task-5 tests
- Regenerate: `just docs` (+ `just resources-docs` if it exists) — generated tool reference, committed
- Test: `tests/mcp/debug/test_debug_ops.py`

**Interfaces:**
- Consumes: `run_engine_op`, `_EngineOp`, `_gdbmi_maturity`, `_docmeta`, Task 3/4 engine ops.
- Produces: `_list_modules_op(session_id)`, `_load_module_symbols_op(session_id, module, expected_base)`; `_register_debug_list_modules` (read-only), `_register_debug_load_module_symbols` (mutating), both appended to `_register_debug_ops`.

- [ ] **Step 1: Write failing tool-level tests** via `run_engine_op` on a seeded `live` session (mirror `test_set_watchpoint_returns_watching:689`): `debug.list_modules` → `status="listed"`, `data={count, truncated, decode_errors, modules:[...]}`; `debug.load_module_symbols` → `status="loaded"`, `data={module, base_address, symbols_loaded, identity_verified}`; and a `stale_module_address` failure envelope (`error_category="debug_attach_failure"`, `data["code"]`).
- [ ] **Step 2:** Run `pytest tests/mcp/debug/test_debug_ops.py -k module -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the two op factories (`list` reads the `GdbModuleList`, dumps `result.modules` rows via `model_dump(mode="json", exclude_none=True)`, sets `data={count: len(result.modules), truncated: result.truncated, decode_errors: result.decode_errors, modules: [...]}`, next `["debug.load_module_symbols","debug.backtrace"]`; `load` dumps the returned `GdbModule` into `data={module, base_address, symbols_loaded, identity_verified}`, next `["debug.backtrace","debug.disassemble","debug.list_modules"]`), the two registrations (`expected_base: int | None = None`), and append to `_register_debug_ops`. **In the same commit**, add both tools to `_TOOL_SCOPES` (`_CONTRIBUTOR`), `tool_index` vocab, and `_BEHAVIOR_TESTS_BY_TOOL`; regenerate `just docs`.
- [ ] **Step 4:** Run the tool tests + the completeness/doc guards (`pytest tests/mcp/core/test_tool_docs.py tests/mcp/core/test_app.py -q`) + `just lint` + `just type` — Expected: PASS.
- [ ] **Step 5:** Commit `feat(923): debug.list_modules + load_module_symbols tools + wiring`.

---

## Self-Review

**Spec coverage:**
- Criterion 1 (list name/base/symbols_loaded) → Task 3 + Task 5. ✓
- Criterion 2 (structured load tool) → Task 4 + Task 5. ✓
- Criterion 3 (missing debuginfo → configuration_error + remediation) → Task 2 + Task 4 step 1(e). ✓
- Criterion 4 (stale → categorized, no silent wrong load; address + binary identity) → Task 4 steps 1(c)(d)(f)(g). ✓
- Criterion 5 (tests: list, load, missing debuginfo, stale, malformed MI) → Tasks 2–5 test matrices, incl. `module_decode_failed` malformed-MI (Task 3 1(c)(e)). ✓
- ADR wiring (scopes/vocab/docs/guards, fault-inject, shared engine) → Tasks 3–5. ✓

**Green-at-every-commit (the prior review's findings):** Task 1 is additive-only (no Protocol widening) → green. Each Protocol method lands with its engine + fault-inject impl (Tasks 3, 4) → `ty` green per commit. Tool registration lands with scopes/vocab/behavior-map/docs (Task 5) → completeness/doc guards green per commit. `kernel_ref_for_run_sync` is added in Task 2 (only `debuginfo_ref_for_run_sync` exists today). Base is `int` end-to-end (Task 3 interface).

**Placeholder scan:** code shown as representative sketches because this session implements the plan with TDD (tightly-coupled change); exact file/symbol names verified against the codebase during each task's red-step. No `TODO`/"handle edge cases" left as a deliverable.

**Type consistency:** `GdbModule`/`GdbModuleList` fields, `list_modules(...) -> GdbModuleList` / `load_module_symbols(...) -> GdbModule` signatures, `ModuleDebuginfo`, the `module_debuginfo_resolver` seam (wired at both composition sites), and the `_module_walk`(int base, returns rows+truncated+decode_errors)/`_module_base_field`/`_verify_identity` helpers are used consistently across Tasks 1→5.
