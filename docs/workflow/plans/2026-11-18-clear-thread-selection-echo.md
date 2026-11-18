# Clear the CPU selection and echo the read thread (#3691) — plan

Goal: implement [the spec](../specs/2026-11-18-clear-thread-selection-echo-design.md) and
[ADR-0824](../../adr/0824-clear-the-cpu-selection-and-echo-the-read-thread.md).

Global Constraints: no dependency, schema, migration or new `Operation` literal; results of
sessions that never select a thread differ only by `thread_id`; reads other than the two named
are unchanged.

Expected implementation size: 250 changed lines (M) — port ~25, engine ~25, fault-inject ~10,
MCP ops and docstrings ~60, tests ~100, live cells ~30, generated docs ~20.

## Task 1 — port, engine, fault-inject

Files: `src/kdive/providers/ports/debug.py`, `.../gdbmi/commands/threads.py`,
`.../gdbmi/commands/stack.py`, `src/kdive/providers/fault_inject/debug/gdb.py`,
`tests/providers/local_libvirt/test_debug_gdbmi.py`, `tests/providers/fault_inject/test_provider.py`.
Steps: `select_thread` accepts `None` and returns `None`; move `_current_thread` to
`GdbMiThreadCommands.read_thread_id` and add it to the port; `read_frame` calls it; fault-inject
mirrors both. Verify: focused tests for clear, clear-then-read omits `--thread`, `read_thread_id`
with and without a selection.

## Task 2 — MCP tools

Files: `src/kdive/mcp/tools/debug/operations/{threads,memory,stack}.py`,
`tests/mcp/debug/test_debug_ops.py`. Steps: `thread_id: int | None`, `cleared` result; echo
`thread_id` in the register and backtrace `data`; rewrite the docstrings and `Field` text.
Verify: MCP tests for clear, echo with a selection, echo without one.

## Task 3 — generated surfaces and live cells

Files: generated tool reference, CLI verb, toolset docs and resource content (regenerated),
`tests/integration/live_stack/debug_cells.py`. Steps: run the generators; extend
`select_thread_body` to clear and assert the echo. Verify: `just docs-check`, `just
cli-verbs-check`, `just mcp-spec-check`, then the full `just ci` before the first push.
