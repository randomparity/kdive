# gdbstub CPU selection (#924) — plan

Goal: implement [the spec](../specs/2026-11-17-gdbstub-thread-selection-design.md) and
[ADR-0819](../../adr/0819-gdbstub-thread-selection.md). The shared gdb/MI engine lists threads
(`-thread-info`) and holds one selected thread on the attachment that register and stack reads
pass as `--thread N`; two MCP tools publish them. Python 3.14,
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
`.../commands/registers.py`, `.../commands/stack.py`,
`src/kdive/providers/shared/debug_common/gdbmi/core/engine.py` (add mixin),
`src/kdive/providers/fault_inject/debug/gdb.py`, tests
`tests/providers/local_libvirt/test_debug_gdbmi.py`, `tests/providers/fault_inject/test_provider.py`;
ADR status line.

Interfaces: produces `GdbThread`, `GdbThreadList`, `GdbMiAttachment.selected_thread`,
`GdbMiEngine.list_threads(attachment) -> GdbThreadList`,
`GdbMiEngine.select_thread(attachment, *, thread_id: int) -> GdbThread`,
`thread_rows(records: list[MiRecord]) -> list[dict[str, Any]] | None`, `MAX_THREADS = 128`.
Consumes `execute_mi_command`, `_redactor`, `result_payload_dict`, `_dict_rows`, `config_error`
(`gdbmi/_errors.py`), `_stack_command`, and the test helpers `_FakeMiController`,
`_attachment`, `_engine`, `_frame_controller` in the test module. Scripted rows use the real
QEMU shape verified with gdb 17.2: `{id: "2", target-id: "Thread 1.2", details: "CPU#1 [halted ]",
state: "stopped", frame: {level, addr, func}}`, no `name`.

Verification:
- list — focused-test `test_list_threads_returns_threads_and_current`: two rows and
  `current-thread-id` "2"; asserts ids `["1","2"]`, `current_thread_id == "2"`,
  `selected_thread_id is None`, `details` kept. Red: `AttributeError: ... 'list_threads'`.
- list bound — `test_list_threads_truncates_at_max`: 130 rows give 128 and `truncated is True`.
- malformed list — `test_list_threads_malformed` (parametrized: payload without `threads`, a
  row with `id` "x", empty list): `malformed_thread_info`, `malformed_thread_info`,
  `no_threads`, category `DEBUG_ATTACH_FAILURE`.
- select — `test_select_thread_records_selection`: `-thread-info 2` answers one row; asserts the
  returned row id, `attachment.selected_thread == "2"`.
- register read after selection — `test_select_thread_then_reads_pass_thread`: select 2, then
  `read_registers(["rip"])`, `backtrace` and `read_frame(level=0)`; asserts `controller.written`
  holds `-data-list-register-values --thread 2 x`, `-stack-list-frames --thread 2` and a
  `-stack-list-variables --thread 2 --frame 0 --simple-values`; and with nothing selected the
  commands are the existing ones (existing tests stay green).
- bad id — `test_select_thread_rejects_bad_id` (parametrized `0`, `-1`, `True`, `"2"`):
  `CONFIGURATION_ERROR` / `bad_thread_id`, `controller.written` empty.
- unknown — `test_select_thread_unknown_thread`: `-thread-info 9` answers `threads=[]`;
  `DEBUG_ATTACH_FAILURE` / `unknown_thread`, selection unchanged.
- stale — `test_read_with_stale_selection_is_unknown_thread`: selection "3" set on the
  attachment; the read command answers `^error,msg="Invalid thread id: 3"`; `unknown_thread`
  for `read_registers` and `backtrace`; selection kept.
- fault-inject — `test_debug_engine_threads` in `tests/providers/fault_inject/test_provider.py`:
  `list_threads` one thread "1"; `select_thread(thread_id=1)` returns "1"; `thread_id=2` raises
  `unknown_thread`.

Green: `uv run pytest tests/providers/local_libvirt/test_debug_gdbmi.py
tests/providers/fault_inject/test_provider.py -k "thread or read_frame or backtrace or register"`
→ all selected pass.

## Task 2 — MCP tools, registries, docs, coverage rows

Files: `src/kdive/mcp/tools/debug/operations/threads.py` (new),
`src/kdive/mcp/tools/debug/operations/registrar.py`, `src/kdive/mcp/exposure.py`,
`src/kdive/mcp/schema/tool_index.py`, `src/kdive/mcp/resources/_content/toolsets-debug.md`,
`docs/guide/toolsets/debug.md`, `docs/guide/safety-and-rbac.md`,
`scripts/coverage_campaign/obligations.toml` (owner-2813 debug group: the two tools in tools and
capabilities), `tests/integration/live_stack/debug_cells.py` and `test_debug_cells.py` (tool
registry, gates, cell bodies and the cell count the registry gate asserts), tests
`tests/mcp/debug/test_debug_ops.py`, `tests/mcp/core/test_tool_docs.py`,
`tests/mcp/core/test_exposure.py`, `tests/mcp/core/test_app.py` (expected tool lists, as the
gates dictate); regenerated `docs/guide/reference/*.md`,
`src/kdive/cli/commands/_generated_verbs.py`, resource snapshots, doc constants.

Interfaces: consumes Task 1's engine methods; `run_engine_op_with_resolver`, `_EngineOp`,
`_gdbmi_maturity`, `_op_audit(tool, **args)`, `_docmeta.read_only()` / `_docmeta.mutating()`;
produces tools `debug.list_threads(session_id)` and `debug.select_thread(session_id,
thread_id: int)`.

Verification:
- list tool — focused-test `test_list_threads_returns_threads` in
  `tests/mcp/debug/test_debug_ops.py` (scripted `-thread-info`): asserts `thread_count == 2`,
  `threads[0]["id"] == "1"`, `selected_thread_id` absent. Red: unknown tool.
- select tool — `test_select_thread_returns_selected`: `status == "selected"`,
  `data["thread_id"] == "2"`, and a following `list_threads` shows `selected_thread_id == "2"`;
  `test_select_thread_unknown_thread_fails`: `error_category` is `debug_attach_failure` and
  `data["code"] == "unknown_thread"`.
- registries — the existing parity gates (`test_tool_docs.py`, `test_exposure.py`,
  `test_app.py`, tool-index coverage, `test_debug_cells.py` cell count) fail until the names are
  added; they are the focused tests for exposure, index and cells.
- docs, CLI verbs, resources, coverage rows — task-test-not-applicable for prose;
  `just docs`, `just cli-verbs`, `just resources-docs` regenerate and `just docs-check`,
  `just cli-verbs-check`, `just resources-docs-check`, `just coverage-check` gate them.
- live proof — the two live-stack cells are written but not run here; the engine commands were
  probed against `qemu-system-x86_64 -smp 3` with gdb 17.2 during design.

Green: `uv run pytest tests/mcp/debug/test_debug_ops.py tests/mcp/core
tests/integration/live_stack/test_debug_cells.py -k "thread or tool_docs or exposure or app or
cells"` → pass; then `just lint`, `just type`, the check recipes above.
