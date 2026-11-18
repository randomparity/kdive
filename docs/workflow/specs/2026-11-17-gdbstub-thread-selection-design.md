# gdbstub CPU selection (#924)

Decision: [ADR-0819](../../adr/0819-gdbstub-thread-selection.md).

## Problem

Multi-vCPU kernel debugging needs a typed way to list the gdbstub's CPUs and pick one before
`debug.read_registers` and `debug.backtrace`. Today those read gdb's implicit current thread.

## Design

Port (`providers/ports/debug.py`): `GdbThread(id, target_id, name, state, func, addr)`,
`GdbThreadList(threads, current_thread_id)`, `GdbThreadSelection(thread_id, frame)`; the
`GdbMiEngine` protocol gains `list_threads(attachment)` and `select_thread(attachment, *,
thread_id)`.

Engine (new `gdbmi/commands/threads.py`, mixed into `GdbMiEngine`): `list_threads` runs
`-thread-info`; `thread_rows(records)` in `mi_protocol.py` returns the `threads` dict rows (or
`None` when absent or not a list). A `None` result or a row without a string numeric `id` raises
`malformed_thread_info`; an empty list raises `no_threads`; `current-thread-id` is kept only when
numeric. `select_thread` rejects a non-int, bool or `< 1` id with `bad_thread_id` before any MI
command, runs `-thread-select N`, maps gdb's `Thread ID N not known.` to `unknown_thread`
(other gdb errors propagate unchanged), and requires `new-thread-id == str(N)` else
`malformed_thread_select`. Text fields pass the redactor like the stack family.

Fault-inject: one synthetic thread `1`; selecting `1` succeeds, any other id raises
`unknown_thread`.

MCP (`mcp/tools/debug/operations/threads.py`, registered by `registrar.py`):
`debug.list_threads(session_id)` -> `data={current_thread_id?, thread_count, threads}`;
`debug.select_thread(session_id, thread_id)` -> status `selected`, `data={thread_id, frame?}`.
Both are `_CONTRIBUTOR` in `exposure.py`, indexed in `schema/tool_index.py`, covered by
`test_tool_docs.py`, listed in the safety table and toolset docs, and get a coverage obligation
group owned by #924. The wrapper docstrings state the stop-reselection limit, the id range, the
codes, and that `read_registers`/`backtrace`/`read_frame` use the selected thread.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a live gdbstub DebugSession;
   local- and remote-libvirt engines (shared gdbmi code); x86_64 and ppc64le share the path.
2. Invariants: existing debug tool results unchanged; no session-row or schema change; guest
   memory untouched; redaction of guest-derived text.
3. Accepted: selection is lost at the next stop or re-attach (documented, ADR-0819); a thread
   id valid at list time may be gone at select time and answers `unknown_thread`; no live-VM
   proof in this change.
4. Covered elsewhere: per-call thread override, drgn/crash CPU selection and non-QEMU
   providers are unowned exclusions of #924; live debug cells are owned by #2813.

## Success

- S1 `debug.list_threads` returns every gdbstub thread and the current one (criteria 1, 3).
- S2 `debug.select_thread` selects a thread, returns it, and a following `read_registers`
  runs after the `-thread-select` command (criteria 2, 3).
- S3 a non-positive, non-integer or unknown id and malformed MI output fail with the codes
  in ADR-0819 (criteria 4, 5).
- S4 tool reference, CLI verbs, tool index and coverage obligations match the code.

## Validation

- S1 focused-test: `tests/providers/local_libvirt/test_debug_gdbmi.py::test_list_threads_*`.
- S2 focused-test: `test_select_thread_then_read_registers_orders_commands`; MCP
  `tests/mcp/debug/test_debug_ops.py::test_select_thread_returns_selected`.
- S3 focused-test: `test_select_thread_rejects_bad_id`, `test_select_thread_unknown_thread`,
  `test_list_threads_malformed`, `test_select_thread_malformed_reply`; MCP failure case.
- S4 task-test-not-applicable for wording; `just docs-check`, `just cli-verbs-check`,
  `just mcp-spec-check`, `just coverage-check` and `tests/mcp/core/test_tool_docs.py` gate it.
