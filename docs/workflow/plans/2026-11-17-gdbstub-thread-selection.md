# gdbstub CPU selection (#924) — plan

Goal: implement [the spec](../specs/2026-11-17-gdbstub-thread-selection-design.md) and
[ADR-0819](../../adr/0819-gdbstub-thread-selection.md). The shared gdb/MI engine lists threads
(`-thread-info`) and selects one (`-thread-select N`); two MCP tools publish them. Python 3.14,
uv, just, pydantic, FastMCP, pygdbmi.

Global Constraints: no dependency, schema or migration; no new `Operation` literal; existing
debug tool signatures and results unchanged; thread ids are positive integers rendered as
strings; ADR-0819 flips to `Accepted (<merge date>)` in Task 1.

Expected implementation size: 230–300 changed lines (M) — port ~35, mi_protocol ~8, engine
commands ~90, fault-inject ~15, MCP op ~90, tests ~150, registry rows and docs ~30.

## Task 1 — port, engine, fault-inject engine

Files: `src/kdive/providers/ports/debug.py`,
`src/kdive/providers/shared/debug_common/gdbmi/core/mi_protocol.py`,
`src/kdive/providers/shared/debug_common/gdbmi/commands/threads.py` (new),
`src/kdive/providers/shared/debug_common/gdbmi/core/engine.py` (add mixin),
`src/kdive/providers/fault_inject/debug/gdb.py`, tests
`tests/providers/local_libvirt/test_debug_gdbmi.py`, `tests/providers/fault_inject/test_provider.py`;
ADR status line.

Interfaces: produces `GdbThread`, `GdbThreadList`, `GdbThreadSelection`,
`GdbMiEngine.list_threads(attachment) -> GdbThreadList`,
`GdbMiEngine.select_thread(attachment, *, thread_id: int) -> GdbThreadSelection`,
`thread_rows(records: list[MiRecord]) -> list[dict[str, Any]] | None`. Consumes
`execute_mi_command`, `_redactor`, `result_payload_dict`, `_dict_rows`, `config_error`
(`gdbmi/_errors.py`), `GdbFrame`, and the fake controller helpers `_FakeMiController`,
`_attachment`, `_engine` in the test module.

Verification:
- list — focused-test `test_list_threads_returns_threads_and_current`: `-thread-info` scripted
  with two rows (`id` "1"/"2", `target-id`, `state`, `frame` with `func`/`addr`) and
  `current-thread-id` "2"; asserts ids `["1","2"]`, `current_thread_id == "2"`. Red:
  `AttributeError: 'GdbMiEngine' object has no attribute 'list_threads'`.
- malformed list — `test_list_threads_malformed` (parametrized: payload without `threads`,
  a row with `id` "x", empty list): codes `malformed_thread_info`, `malformed_thread_info`,
  `no_threads`, category `DEBUG_ATTACH_FAILURE`.
- select — `test_select_thread_selects_and_returns_frame`: `-thread-select 2` scripted with
  `new-thread-id` "2" and a frame; asserts `thread_id == "2"`, `frame.func`.
- register read after selection — `test_select_thread_then_read_registers_orders_commands`:
  select 2 then `read_registers(["rip"])` on one controller; asserts `controller.written`
  has `-thread-select 2` before `-data-list-register-values x`.
- bad id — `test_select_thread_rejects_bad_id` (parametrized `0`, `-1`, `True`, `"2"`): raises
  `CONFIGURATION_ERROR` / `bad_thread_id` and `controller.written` stays empty.
- unknown — `test_select_thread_unknown_thread`: the command answers
  `^error,msg="Thread ID 9 not known."`; `DEBUG_ATTACH_FAILURE` / `unknown_thread`, details
  carry `thread_id` 9. An unrelated `^error` propagates with its original details.
- malformed reply — `test_select_thread_malformed_reply`: `^done` with no `new-thread-id`, and
  with `new-thread-id` "3" for a request of 2: `malformed_thread_select`.
- fault-inject — `test_debug_engine_threads` in `tests/providers/fault_inject/test_provider.py`:
  `list_threads` one thread "1"; `select_thread(thread_id=1)` returns "1"; `thread_id=2` raises
  `unknown_thread`.

Green: `uv run pytest tests/providers/local_libvirt/test_debug_gdbmi.py
tests/providers/fault_inject/test_provider.py -k thread` → all selected pass.

## Task 2 — MCP tools, registries, docs, coverage rows

Files: `src/kdive/mcp/tools/debug/operations/threads.py` (new),
`src/kdive/mcp/tools/debug/operations/registrar.py`, `src/kdive/mcp/exposure.py`,
`src/kdive/mcp/schema/tool_index.py`, `src/kdive/mcp/resources/_content/toolsets-debug.md`,
`docs/guide/toolsets/debug.md`, `docs/guide/safety-and-rbac.md`,
`scripts/coverage_campaign/obligations.toml`, tests `tests/mcp/debug/test_debug_ops.py`,
`tests/mcp/core/test_tool_docs.py`, `tests/mcp/core/test_exposure.py`, `tests/mcp/core/test_app.py`
(expected tool lists, as the gates dictate); regenerated `docs/guide/reference/*.md`,
`src/kdive/cli/commands/_generated_verbs.py`, resource snapshots, doc constants.

Interfaces: consumes Task 1's engine methods; `run_engine_op_with_resolver`, `_EngineOp`,
`_gdbmi_maturity`, `_docmeta.read_only()`; produces tools `debug.list_threads(session_id)` and
`debug.select_thread(session_id, thread_id: int)`.

Verification:
- list tool — focused-test `test_list_threads_returns_threads` in
  `tests/mcp/debug/test_debug_ops.py` (scripted `-thread-info`): asserts
  `resp.data["current_thread_id"]`, `thread_count == 2`, `threads[0]["id"] == "1"`. Red:
  `KeyError`/unknown tool.
- select tool — `test_select_thread_returns_selected`: asserts `status == "selected"`,
  `data["thread_id"] == "2"`; `test_select_thread_unknown_thread_fails`: `error_category` is
  `debug_attach_failure` and `data["code"] == "unknown_thread"`.
- registries — the existing parity gates (`test_tool_docs.py`, `test_exposure.py`,
  `test_app.py`, tool-index coverage) fail until the two names are added; they are the
  focused tests for exposure and index.
- docs, CLI verbs, resources, coverage rows — task-test-not-applicable for prose;
  `just docs`, `just cli-verbs`, `just resources-docs` regenerate and `just docs-check`,
  `just cli-verbs-check`, `just resources-docs-check`, `just coverage-check` gate them.

Green: `uv run pytest tests/mcp/debug/test_debug_ops.py tests/mcp/core -k "thread or tool_docs or
exposure or app"` → pass; then `just lint`, `just type`, the check recipes above.
