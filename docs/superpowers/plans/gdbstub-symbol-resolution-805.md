# Plan — symbol resolution over the gdbstub transport (#805)

Derived from [the spec](../../specs/2026-06-25-gdbstub-symbol-resolution.md) and
[ADR-0248](../../adr/0248-gdbstub-symbol-resolution.md). The work is one cohesive contract
expansion (engine method + MCP tool + wiring), so it is implemented in this session with TDD,
not handed to independent subagents. Tasks are ordered.

**Commit grouping (keeps every commit green + bisectable).** Tasks map to two logical
commits, not six:

- **Commit A — engine capability (Tasks 1–3):** the `evaluate_value` helper, the
  `GdbMiEngine.resolve_symbol` method + tests, and the `GdbMiEngine` Protocol method. This
  commit is self-contained and green on its own (no tool is registered yet, so no exposure or
  doc gate is affected).
- **Commit B — MCP exposure (Tasks 4–6) as ONE commit:** registering `debug.resolve_symbol`
  (Task 4) flips `test_exposure_map_covers_every_registered_tool` red until the exposure entry
  (Task 5) exists, and flips `just docs-check`/`resources-docs-check` red until the references
  are regenerated (Task 6). So the registration, its `_CONTRIBUTOR` map entry, and the
  regenerated `debug.md` + resource snapshot MUST land in the same commit. Do **not** commit
  Task 4 alone — it would be a knowingly-red, non-bisectable commit.

Within each commit, write the failing test first (TDD); stage all of that commit's files only
once its full guardrail subset is green.

**Guardrail commands** (run the relevant subset before each commit; full suite before push):

```
just lint        # ruff check + ruff format --check
just type        # ty check, whole tree (src + tests)
uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -q
uv run python -m pytest tests/mcp/debug/test_debug_ops.py tests/mcp/core/test_app.py -q
just docs        # regenerate docs/guide/reference/debug.md
just resources-docs   # regenerate packaged doc-resource snapshot
just docs-check resources-docs-check adr-status-check   # CI gates
```

Conventions (from `CLAUDE.md`/`AGENTS.md`): uniform `ToolResponse` envelope with a literal
`suggested_next_actions` list and an `error_category` only on failure; most-specific
`ErrorCategory`; ruff line length 100; absolute imports; `ty` strict; tests mirror the package
tree. Doc prose stays plain (no "robust"/"comprehensive"/etc.).

---

## Task 1 — `mi_protocol.evaluate_value` helper (parse `value` from a result record)

**Where it fits:** the engine needs the `value` string from a
`-data-evaluate-expression` result. `mi_protocol.py` already holds the per-command parse
helpers (`breakpoint_rows`, `register_values_by_number`, `memory_segments`); add a sibling.

**Files:** `src/kdive/providers/shared/debug_common/mi_protocol.py`,
`tests/providers/local_libvirt/test_debug_gdbmi.py` (or the mi_protocol test module if one
exists — confirm; engine tests import these helpers, so a unit test alongside the engine tests
is acceptable).

**TDD:**
1. Failing test: `evaluate_value([result record with payload {"value": "(int *) 0x10 <s>"}])`
   returns the string `"(int *) 0x10 <s>"`; a record with no `value` key returns `None`; a
   non-result record list returns `None`.
2. Implement:
   ```python
   def evaluate_value(records: list[MiRecord]) -> str | None:
       value = result_payload_dict(records).get("value")
       return value if isinstance(value, str) else None
   ```

**Acceptance:** helper returns the `value` string or `None`; no exception on malformed input.

**Rollback:** delete the helper + its test.

---

## Task 2 — `GdbMiEngine.resolve_symbol` engine method (TDD, no gdb)

**Where it fits:** the core capability. Lives on the **shared** engine
(`providers/shared/debug_common/gdbmi.py`) so remote-libvirt inherits it. Mirrors the existing
`set_breakpoint`/`read_memory` shape (name-shape gate before any MI command; CategorizedError
contract).

**Files:** `src/kdive/providers/shared/debug_common/gdbmi.py`,
`tests/providers/local_libvirt/test_debug_gdbmi.py`.

**Design (from ADR-0248):**
- Add module constants near the existing regexes:
  - reuse `_SYMBOL_NAME_RE` for the name gate (already a bare C identifier).
  - `_SYMBOL_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")` — first-match search (skips a leading
    `(int *)` type cast; the address precedes any `<symbol>` annotation in gdb's rendering).
- Method:
  ```python
  def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int:
      if not _SYMBOL_NAME_RE.match(name):
          raise _config_error(
              f"symbol name must be a bare C identifier, got {name!r}",
              code="bad_symbol_name",
              details={"name": name},
          )
      records = self.execute_mi_command(attachment, f"-data-evaluate-expression &{name}")
      value = evaluate_value(records)
      match = _SYMBOL_ADDR_RE.search(value) if isinstance(value, str) else None
      if match is None:
          raise CategorizedError(
              "gdb/MI returned no parseable symbol address",
              category=ErrorCategory.DEBUG_ATTACH_FAILURE,
              details={
                  "code": "bad_symbol_value",
                  "name": name,
                  "value": self._redactor().redact_value(value),
              },
          )
      return int(match.group(0), 16)
  ```
- Update the module docstring (lines 1-19) and the engine class docstring: the engine now
  evaluates exactly one gated form, `&<identifier>` (address-of-a-name) — narrowing, not
  reversing, the "expression evaluation is outside this engine's contract" statement. Note the
  op is run-state independent (a symtab lookup, valid whether or not the inferior is stopped)
  and that an addressless known symbol (enum/macro constant) surfaces as `DEBUG_ATTACH_FAILURE`
  like an unknown symbol.
- Bump "seven Debug-plane ops" → "eight" in the docstrings.

**TDD (each a failing test first, against the scripted `_FakeMiController`):**
1. data-global: response for `-data-evaluate-expression &d_hash_shift` →
   `{"value": "(int *) 0xffffffff82a1b3c0 <d_hash_shift>"}`; assert returns
   `0xffffffff82a1b3c0`; assert the issued command is `-data-evaluate-expression &d_hash_shift`.
2. function symbol: `{"value": "(void (*)(void)) 0xffffffff81000000 <panic>"}` → returns
   `0xffffffff81000000`.
3. bare `0x0` value (weak/absent) → returns `0`.
4. plain `0xdead` value (no cast, no annotation) → returns `0xdead`.
5. non-identifier name (`"d_hash_shift; rm -rf /"`) → `CategorizedError`,
   `CONFIGURATION_ERROR`, `details == {"code": "bad_symbol_name", "name": ...}`, and
   `controller.written == []` (no MI command issued).
6. gdb `^error` (response `{"type": "result", "message": "error", "payload": {"msg": "No symbol"}}`)
   → `DEBUG_ATTACH_FAILURE` via `execute_mi_command` (assert category + command in details).
7. missing `value` key in payload → `DEBUG_ATTACH_FAILURE`, `code == "bad_symbol_value"`.
8. unparseable `value` (e.g. `"void"`, no `0x`) → `DEBUG_ATTACH_FAILURE`,
   `code == "bad_symbol_value"`; with a registered secret in the value, assert the echoed
   `details["value"]` is redacted.

**Acceptance:** all eight tests green; `just type` clean; the `&name` command string and the
no-command-on-bad-name property are asserted.

**Rollback:** delete the method, constants, and tests; revert the docstring edits.

---

## Task 3 — `GdbMiEngine` Protocol: add `resolve_symbol`

**Where it fits:** `providers/ports/debug.py` declares the `GdbMiEngine` Protocol the ops layer
types against. The concrete method must be declared there or `ty` will reject the op call.

**Files:** `src/kdive/providers/ports/debug.py`.

**Change:** add to the `GdbMiEngine` Protocol, matching the existing method docstring style:
```python
def resolve_symbol(self, attachment: GdbMiAttachment, name: str) -> int:
    """Resolve a bare C symbol name to its address through gdb/MI.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-identifier name,
            ``DEBUG_ATTACH_FAILURE`` for a gdb error or an unparseable address value, or
            ``INFRASTRUCTURE_FAILURE`` for command timeouts.
    """
    ...
```

**Acceptance:** `just type` clean; the op layer can call `engine.resolve_symbol(...)` without a
type error.

**Rollback:** remove the Protocol method.

---

## Task 4 — `debug.resolve_symbol` MCP op + registration (TDD)

**Where it fits:** `mcp/tools/debug/ops.py` holds the op factories and tool registrations.
Add an eighth op following the `_read_memory_op` / `_register_debug_read_memory` pattern.

**Files:** `src/kdive/mcp/tools/debug/ops.py`,
`tests/mcp/debug/test_debug_ops.py`.

**Change:**
- Op factory:
  ```python
  def _resolve_symbol_op(session_id: str, name: str) -> _EngineOp:
      def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
          address = engine.resolve_symbol(attachment, name)
          return ToolResponse.success(
              session_id,
              "resolved",
              suggested_next_actions=["debug.read_memory", "debug.read_registers"],
              data={"symbol": name, "address": f"0x{address:x}"},
          )
      return op
  ```
- Register `debug.resolve_symbol` with `_docmeta.read_only()` + `_gdbmi_maturity()`, a
  `session_id` and `name` Field-annotated param (name description: "Bare C global/function
  symbol name to resolve to its address."); call `_register_debug_resolve_symbol` from
  `_register_debug_ops`.
- Update the module docstring (the seven-op list) and the `_gdbmi_maturity` docstring
  ("All seven" → "All eight").

**TDD (drive `run_engine_op` against a seeded `live` DebugSession + fake attach seam):**
1. happy path: controller response for `-data-evaluate-expression &d_hash_shift` →
   `{"value": "(int *) 0x1234 <d_hash_shift>"}`; assert `resp.status == "resolved"`,
   `resp.data["address"] == "0x1234"`, `resp.data["symbol"] == "d_hash_shift"`,
   `"debug.read_memory" in resp.suggested_next_actions`.
2. bad name → `resp.status == "error"`, `resp.error_category == "configuration_error"`,
   `resp.data["code"] == "bad_symbol_name"`, `attach.controller.written == []`.
3. add `"resolve_symbol": debug_ops._resolve_symbol_op` to the `_op_for` factory dict so the
   shared helper can build it.

**Acceptance:** both op tests green; the op appears in `_register_debug_ops`.

**Rollback:** remove the factory, the registration, the `_op_for` entry, and the tests.

---

## Task 5 — RBAC exposure entry

**Where it fits:** `mcp/exposure.py` maps every tool to its required role.
`test_exposure_map_covers_every_registered_tool` (`tests/mcp/core/test_app.py`) fails if a
registered tool has no entry — so this task is both required and self-verifying.

**Files:** `src/kdive/mcp/exposure.py`.

**Change:** add `"debug.resolve_symbol": _CONTRIBUTOR,` in the `# debug` block (alongside the
other `debug.*` contributor entries).

**Acceptance:** `uv run python -m pytest tests/mcp/core/test_app.py -q` green (the completeness
guard passes).

**Rollback:** remove the map entry.

---

## Task 6 — Regenerate generated references

**Where it fits:** `docs/guide/reference/debug.md` is generated from the registered tools and
gated by `just docs-check`; the packaged doc-resource snapshot is gated by
`just resources-docs-check`. Both must be regenerated and committed.

**Files (generated — do not hand-edit):** `docs/guide/reference/debug.md`, any packaged
snapshot under the resource content tree that mirrors it.

**Steps:**
1. `just docs` → regenerate the tool reference; confirm `debug.resolve_symbol` appears.
2. `just resources-docs` → regenerate the packaged snapshot.
3. `just docs-check resources-docs-check` → both clean.

**Acceptance:** `just docs-check` and `just resources-docs-check` pass; the diff shows only the
new `debug.resolve_symbol` rows.

**Rollback:** `git checkout` the generated files.

---

## Final verification (before push)

Run the full local gate (architecture/boundary/doc tests live outside the touched dirs):

```
just lint && just type && just ci
```

`just ci` covers lint, type, lock-check, shell/ansible/workflow lint, mermaid, docs-links,
docs-paths, adr-status-check, docs-check, config-docs-check, config-guard, env-docs-check,
resources-docs-check, chart-version-check, and the test suite. `live_vm`/`live_stack` markers
stay skipped (no gdb/socket needed — every op test drives the scripted fake controller).

## Risks & mitigations

- **gdb value rendering variance:** the parser searches for the first `0x` token rather than
  anchoring, tolerating a type-cast prefix; a missing token falls to `bad_symbol_value`. The
  live path is unit-tested only via the fake; real-gdb rendering is exercised by the existing
  `live_vm` attach seam, unchanged here.
- **Forgotten wiring:** the exposure completeness guard and the doc-generation gates make a
  missed registration/doc a red guardrail, not a silent gap.
