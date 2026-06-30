# gdb Module-Symbol Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This change is tightly coupled (ports → engine → fault-inject → ops/tools → wiring); execute the tasks in order in one session.

**Goal:** Add `debug.list_modules` and `debug.load_module_symbols` to the shared gdb-MI debug tier so an agent can enumerate loaded kernel modules and load a module's symbols over a live gdbstub `DebugSession`.

**Architecture:** Two ops on the shared `GdbMiEngine` (remote-libvirt inherits free) + two `contributor` MCP tools, mirroring ADR-0275/0276/0277. Enumeration walks the kernel `modules` list via internally-constructed `-data-evaluate-expression` casts (non-injectable). Loading re-reads the base fresh, verifies binary identity, resolves the `.ko` from the published `kernel_ref` tar via an injected resolver, and `add-symbol-file`s it. No schema/migration/RBAC/destructive-gate change.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`. gdb-MI via the existing `GdbController`/scripted-fake seam. See ADR-0278 and `docs/superpowers/specs/2026-06-29-issue-923-gdb-module-symbols-design.md`.

## Global Constraints

- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, 100-char lines, absolute imports only.
- Pick the most specific existing `ErrorCategory`; never invent strings. New `data["code"]` discriminators are fine.
- All textual MI/record output passes the `Redactor` before response (engine `_redactor()`); module names/identities are non-secret but still redacted like other record fields.
- Every gdb command is constructed from gated/numeric/engine-staged inputs — never caller text (the ADR-0034/0248/0277 non-injectability rule).
- Guardrails before every commit: `just lint` (`ruff check` + `ruff format --check`), `just type` (whole tree), focused `pytest`. Full `just ci` before push.
- Tools marked `implemented` (`_gdbmi_maturity`), **not** added to `_LOCAL_PROVEN_DEBUG_TOOLS`.
- New ADR/spec already committed (ADR-0278). Do not renumber.

---

### Task 1: Port records, Protocol methods, attachment fields

**Files:**
- Modify: `src/kdive/providers/ports/debug.py`

**Interfaces:**
- Produces: `GdbModule(ProviderModel)` with `name: str | None`, `base_address: str | None`, `symbols_loaded: bool | None`, `identity_verified: bool | None` (all default `None` so `exclude_none` drops list-only/load-only fields); `GdbMiEngine.list_modules(attachment, *, max_modules: int) -> list[GdbModule]`; `GdbMiEngine.load_module_symbols(attachment, *, module: str, expected_base: int | None) -> GdbModule`; `GdbMiAttachment` gains `run_id: str = ""` and `loaded_modules: set[str] = field(default_factory=set)`.

- [ ] **Step 1:** Add the `GdbModule` model after `GdbWatchpointRef`. Add `run_id`/`loaded_modules` to the `GdbMiAttachment` dataclass (defaults keep existing fakes constructing it). Add the two Protocol method stubs with Google-style docstrings naming the raised `CategorizedError` codes (`bad_module_name`, `no_module_debuginfo`, `inferior_running`, `module_decode_failed`, `module_not_loaded`, `stale_module_address`, `module_binary_mismatch`, `add_symbol_failed`).
- [ ] **Step 2:** `just type` — Expected: PASS (no implementations yet; Protocol is structural, concrete classes implemented in later tasks may now type-error until done — if so, proceed; the engine/fault-inject tasks resolve it). If `ty` flags the missing concrete methods immediately, continue to Task 3/5 before re-checking.
- [ ] **Step 3:** `git add` the port file; commit `feat(923): add GdbModule record + module Protocol methods`.

---

### Task 2: ModuleDebuginfoResolver (.ko + identity)

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/debuginfo.py`
- Test: `tests/providers/shared/debug_common/test_debuginfo.py` (or the existing debuginfo test module — match the tree)

**Interfaces:**
- Produces: `ModuleDebuginfo(path: Path, srcversion: str | None, build_id: str | None)` (frozen dataclass); `ModuleDebuginfoResolver` with injected seams `read_kernel_ref: Callable[[str], str | None]`, `fetch_object: Callable[[str], bytes]`, and a method `resolve(run_id: str, module: str) -> ModuleDebuginfo` that lazily fetches/extracts the `kernel_ref` tar (cached per run_id), locates `<module>.ko` matching `-`/`_` variants, and reads `.modinfo` `srcversion=` + `.note.gnu.build-id`. Raises `CategorizedError(CONFIGURATION_ERROR, code="no_module_debuginfo")` with remediation when the module/.ko is absent.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write failing tests.** (a) `resolve` returns `ModuleDebuginfo` with the staged `.ko` path + parsed srcversion/build_id for a fixture tar (build a tiny tar in a tmp dir containing `lib/modules/x/foo.ko` whose bytes are a minimal ELF with a `.modinfo` `srcversion=ABC` and a `.note.gnu.build-id` — or, to avoid hand-rolling ELF, factor identity extraction into an injected `read_identity: Callable[[Path], tuple[str|None,str|None]]` seam and assert the orchestration with a fake). (b) absent module → `no_module_debuginfo` with `data["reason"]`/remediation. (c) `-`/`_` name variant match.
- [ ] **Step 2:** Run `pytest tests/.../test_debuginfo.py -k module -v` — Expected: FAIL (resolver undefined).
- [ ] **Step 3: Implement.** Mirror `DebuginfoResolver`: inject the DB read (`read_kernel_ref` via a `kernel_ref_for_run_sync` query — add it to `db/artifact_queries.py` if absent, paralleling `debuginfo_ref_for_run_sync`) and object fetch; stage into a per-run cached temp dir; extract `lib/modules/`; ELF identity via an injected `read_identity` seam whose real impl parses the `.ko` (`.modinfo`/`.note.gnu.build-id`) directly (no new dependency). Real seams `# pragma: no cover - live_vm`.
- [ ] **Step 4:** Run the tests — Expected: PASS. Then `just lint` + `just type`.
- [ ] **Step 5:** Commit `feat(923): ModuleDebuginfoResolver for .ko path + identity`.

---

### Task 3: engine list_modules

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py`
- Modify: `src/kdive/providers/shared/debug_common/mi_protocol.py` (only if a new parse helper is warranted; prefer `evaluate_value`)
- Test: `tests/.../test_gdbmi.py` (the existing engine unit test module) or via the ops test file's scripted controller

**Interfaces:**
- Consumes: `GdbModule`, Protocol from Task 1; `evaluate_value`, `execute_mi_command`, `_redactor`, `_config_error`, `_RUNNING_RE`.
- Produces: `GdbMiEngine.list_modules(self, attachment, *, max_modules=MAX_MODULES) -> list[GdbModule]`; module-constant `MAX_MODULES = 512`; private `_module_walk(attachment) -> list[tuple[name, base, raw_node]]` and `_module_base_field(attachment, first_node) -> str` (the one-time probe) reused by Task 4.

- [ ] **Step 1: Write failing tests** against the scripted `MiController` fake: (a) two-module walk (head read, two `container_of`/name/base evals, terminator back to `&modules`) → two `GdbModule`s with `base_address` set, `symbols_loaded=False`; assert the `mem[MOD_TEXT].base` probe was used. (b) `truncated`/bound when the walk exceeds `max_modules` (pass a small `max_modules`). (c) one garbage row → skipped, `decode_errors=1`, other row present. (d) running-target `^error` on the head read → `inferior_running`. (e) both base-field probes `^error` → `module_decode_failed`.
- [ ] **Step 2:** Run the tests — Expected: FAIL (`list_modules` undefined).
- [ ] **Step 3: Implement** the walk: read `&modules`/`modules.next`, derive `<list-offset>` via `&((struct module *)0)->list`, loop bounded by `max_modules` constructing `container_of` casts, read `((struct module *)<p>)->name` and the probed base field; catch per-row gdb `^error`/parse failure → skip + count; classify head/running errors. Return redacted `GdbModule`s. Keep `list_modules` ≤100 lines / complexity ≤8 by extracting `_module_walk`/`_module_base_field`.
- [ ] **Step 4:** Run tests — Expected: PASS. `just lint` + `just type`.
- [ ] **Step 5:** Commit `feat(923): engine list_modules kernel module-list walk`.

---

### Task 4: engine load_module_symbols

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py`
- Modify: `src/kdive/providers/ports/debug.py` (engine `__init__` gains injected `module_debuginfo_resolver` seam — default raises `MISSING_DEPENDENCY`, like `attach`) — actually the seam lives on the concrete `GdbMiEngine.__init__` in gdbmi.py, not the Protocol.
- Test: same engine unit test module

**Interfaces:**
- Consumes: Task 1 Protocol; Task 2 `ModuleDebuginfoResolver`/`ModuleDebuginfo`; Task 3 `_module_walk`; `_mi_path`, `execute_mi_command`.
- Produces: `GdbMiEngine.load_module_symbols(self, attachment, *, module, expected_base=None) -> GdbModule`; engine `__init__` param `module_debuginfo_resolver: Callable[[str, str], ModuleDebuginfo] | None = None` (default a callable raising `MISSING_DEPENDENCY`; tests inject a fake). `_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")`.

- [ ] **Step 1: Write failing tests** (scripted controller + fake resolver): (a) success — fresh base read, fake resolver returns `.ko`+matching `srcversion`, assert `add-symbol-file <ko> 0x<base>` issued via `-interpreter-exec console`, result `symbols_loaded=True identity_verified=True`, module in `attachment.loaded_modules`. (b) `bad_module_name` for `"foo;rm"` — no MI issued. (c) module absent from walk → `module_not_loaded`, no add-symbol-file. (d) `expected_base` ≠ current → `stale_module_address`, no add-symbol-file. (e) resolver raises `no_module_debuginfo` → propagates (CONFIGURATION_ERROR + remediation). (f) `mod->srcversion` ≠ resolver srcversion → `module_binary_mismatch`, no add-symbol-file. (g) both identity reads `^error` → loads, `identity_verified=False`; and build_id-fallback match path. (h) idempotent re-load (module already in `loaded_modules`, still present) → returns `loaded`, no second add-symbol-file.
- [ ] **Step 2:** Run — Expected: FAIL.
- [ ] **Step 3: Implement** in the step order from the spec: gate name → re-walk base (reuse `_module_walk`) + staleness → idempotency short-circuit (after staleness passes) → resolver → identity read+compare → `-interpreter-exec console "add-symbol-file ..."` (wrap `^error` → `add_symbol_failed`) → record in `loaded_modules`. Extract `_verify_identity` helper to keep complexity ≤8.
- [ ] **Step 4:** Run — Expected: PASS. `just lint` + `just type`. Update the gdbmi.py module docstring line "general expression evaluation and module loading remain outside this engine's contract" → module loading now in-contract (ADR-0278).
- [ ] **Step 5:** Commit `feat(923): engine load_module_symbols with identity verification`.

---

### Task 5: FaultInjectDebugEngine synthetic impls

**Files:**
- Modify: `src/kdive/providers/fault_inject/debug/gdb.py`
- Test: existing fault-inject debug test (match the tree)

**Interfaces:**
- Consumes: Task 1 Protocol.
- Produces: conforming `list_modules`/`load_module_symbols` returning deterministic synthetic `GdbModule`s (e.g. a fixed `[{name:"fault_inject_demo", base_address:"0x..", symbols_loaded:False}]`; load returns `symbols_loaded=True identity_verified=True`).

- [ ] **Step 1:** Write a test asserting the fault-inject engine satisfies the new Protocol methods with deterministic output (and that the class still type-conforms).
- [ ] **Step 2:** Run — Expected: FAIL.
- [ ] **Step 3:** Implement the two methods mirroring the existing synthetic watchpoint methods' style.
- [ ] **Step 4:** Run + `just type` (this is what makes the Protocol satisfied) — Expected: PASS.
- [ ] **Step 5:** Commit `feat(923): fault-inject engine module-symbol ops`.

---

### Task 6: ops factories + MCP tools

**Files:**
- Modify: `src/kdive/mcp/tools/debug/ops.py`
- Test: `tests/mcp/debug/test_debug_ops.py`

**Interfaces:**
- Consumes: `run_engine_op`, `_EngineOp`, `_gdbmi_maturity`, `_docmeta`, Task 3/4 engine ops.
- Produces: `_list_modules_op(session_id)`, `_load_module_symbols_op(session_id, module, expected_base)`; `_register_debug_list_modules`, `_register_debug_load_module_symbols`; both appended to `_register_debug_ops` (now sixteen tools — update its docstring count).

- [ ] **Step 1: Write failing tool-level tests** via `run_engine_op` on a seeded `live` session (mirror `test_set_watchpoint_returns_watching`): `debug.list_modules` → `status="listed"`, `data={count,truncated,decode_errors,modules:[...]}`; `debug.load_module_symbols` → `status="loaded"`, `data={module,base_address,symbols_loaded,identity_verified}`; and a `stale_module_address` failure envelope (`error_category="debug_attach_failure"`, `data["code"]`).
- [ ] **Step 2:** Run `pytest tests/mcp/debug/test_debug_ops.py -k module -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the two op factories (`list_modules` dumps `model_dump(mode="json", exclude_none=True)` rows; `load` sets next `["debug.backtrace","debug.disassemble","debug.list_modules"]`) and the two registrations (`debug.list_modules` read-only; `debug.load_module_symbols` mutating; `expected_base: int | None = None`). Add to `_register_debug_ops`.
- [ ] **Step 4:** Run the tool tests — Expected: PASS. `just lint` + `just type`.
- [ ] **Step 5:** Commit `feat(923): debug.list_modules + debug.load_module_symbols tools`.

---

### Task 7: Wiring — scopes, vocab, generated docs, guards

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (`_TOOL_SCOPES` → both tools in `_CONTRIBUTOR`)
- Modify: `src/kdive/mcp/tool_index.py` (search vocabulary entries)
- Modify: the `_BEHAVIOR_TESTS_BY_TOOL` coverage map (wherever it lives) for both tools
- Regenerate: `just docs` (+ `just resources-docs` if applicable) — generated tool reference
- Test: run the completeness guards

**Interfaces:**
- Consumes: registered tool names `debug.list_modules`, `debug.load_module_symbols`.

- [ ] **Step 1:** Add both tools to `_TOOL_SCOPES` (`_CONTRIBUTOR`), `tool_index` vocab, and `_BEHAVIOR_TESTS_BY_TOOL` (pointing at the Task 6 tests). Run the exposure/tool_index/behavior completeness guard tests — Expected: they were FAILING for the unmapped tools, now PASS.
- [ ] **Step 2:** Run `just docs` to regenerate the tool reference; review the diff for both tools (descriptions free of ADR-NNNN per the ADR-0270 guard).
- [ ] **Step 3:** `just type` + full `pytest` (the app/registration/no-adr-leak/doc-gen guards live outside the directories edited) — Expected: PASS.
- [ ] **Step 4:** Commit `feat(923): wire module-symbol tools into scopes, vocab, docs`.

---

## Self-Review

**Spec coverage:**
- Criterion 1 (list name/base/symbols_loaded) → Task 3 + Task 6. ✓
- Criterion 2 (structured load tool) → Task 4 + Task 6. ✓
- Criterion 3 (missing debuginfo → configuration_error + remediation) → Task 2 + Task 4 step 1(e). ✓
- Criterion 4 (stale → categorized, no silent wrong load; address + binary identity) → Task 4 steps 1(c)(d)(f)(g). ✓
- Criterion 5 (tests: list, load, missing debuginfo, stale, malformed MI) → Tasks 2–4/6 test matrices, incl. `module_decode_failed` malformed-MI (Task 3 1(e), 1(c)). ✓
- ADR wiring (scopes/vocab/docs/guards, fault-inject, shared engine) → Tasks 5, 7. ✓

**Placeholder scan:** code shown as representative sketches because this session implements the plan with TDD (tightly-coupled change); exact file/symbol names verified against the codebase during each task's red-step. No `TODO`/"handle edge cases" left as the deliverable.

**Type consistency:** `GdbModule` fields, `list_modules`/`load_module_symbols` signatures, `ModuleDebuginfo`, and `module_debuginfo_resolver` seam names are used consistently across Tasks 1→6. The engine `_module_walk`/`_module_base_field` helpers defined in Task 3 are reused in Task 4.
