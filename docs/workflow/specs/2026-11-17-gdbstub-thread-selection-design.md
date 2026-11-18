# gdbstub CPU selection (#924)

Decision: [ADR-0819](../../adr/0819-gdbstub-thread-selection.md).

## Problem

Multi-vCPU kernel debugging needs a typed way to list the gdbstub's CPUs and pick one before
`debug.read_registers` and `debug.backtrace`. Today those read gdb's implicit current thread.

## Design

Port (`providers/ports/debug.py`): `GdbThread(id, target_id, details, name, state, func, addr)`,
`GdbThreadList(threads, truncated, current_thread_id, selected_thread_id)`;
`GdbMiAttachment.selected_thread: str | None = None`; the `GdbMiEngine` protocol gains
`list_threads(attachment)` and `select_thread(attachment, *, thread_id)`.

Engine (new `gdbmi/commands/threads.py`, mixed into `GdbMiEngine`): `list_threads` runs
`-thread-info`; `thread_rows(records)` in `mi_protocol.py` returns the `threads` dict rows or
`None` when absent or not a list. `None` or a row without a numeric string `id` raises
`malformed_thread_info`; an empty list raises `no_threads`; more than 128 rows are cut with
`truncated`. `select_thread` rejects a non-int, bool or `< 1` id with `bad_thread_id` before any
MI command, runs `-thread-info N`, raises `unknown_thread` when no returned row has that id, else
sets `attachment.selected_thread` and returns the row. `read_registers`, `backtrace` and
`read_frame` insert `--thread N` when selected (`-data-list-register-values --thread N x`,
`-stack-list-frames --thread N`, and `read_frame` uses it in place of `-thread-list-ids`); gdb's
`Invalid thread id: N` becomes `unknown_thread`. Text fields pass the redactor.

Fault-inject: one synthetic thread `1`; selecting another id raises `unknown_thread`.

MCP (`mcp/tools/debug/operations/threads.py`, registered by `registrar.py`):
`debug.list_threads(session_id)` (read-only) -> `data={thread_count, truncated,
current_thread_id?, selected_thread_id?, threads}`; `debug.select_thread(session_id, thread_id)`
(mutating, audited via `_op_audit`) -> status `selected`, `data={thread_id, thread}`. Both are
`_CONTRIBUTOR` in `exposure.py`, indexed in `schema/tool_index.py`, in the safety table and
toolset docs, and added to the owner-2813 debug group in `obligations.toml` and the
`live_stack` cell registry. Docstrings state the id range, the codes, and that the selection is
shared per session and ends at re-attach.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a live gdbstub DebugSession;
   local- and remote-libvirt engines (shared gdbmi code); x86_64 and ppc64le share the path.
2. Invariants: existing debug results unchanged when nothing is selected; no schema change;
   guest memory untouched; redaction of guest-derived text.
3. Accepted: the selection is shared by every contributor on the session and ends at
   re-attach; an id valid at list time may be gone later (`unknown_thread`); results do not
   echo the thread; no live-VM proof of the new tools in this change.
4. Covered elsewhere: per-call thread override, drgn/crash CPU selection and non-QEMU providers
   are unowned exclusions of #924; live debug cells are owned by #2813.

## Success

- S1 `debug.list_threads` returns the gdbstub threads (at most 128) with gdb's current and the
  selected id (criteria 1, 3).
- S2 `debug.select_thread` records a thread and reports it; the next `read_registers` and
  `backtrace` pass `--thread N` (criteria 2, 3).
- S3 a non-positive, non-integer, unknown or stale id and malformed MI output fail with the
  codes in ADR-0819 (criteria 4, 5).
- S4 tool reference, CLI verbs, tool index and coverage obligations match the code.

## Validation

- S1 focused-test: `tests/providers/local_libvirt/test_debug_gdbmi.py::test_list_threads_*`.
- S2 focused-test: `test_select_thread_then_reads_pass_thread`; MCP
  `tests/mcp/debug/test_debug_ops.py::test_select_thread_returns_selected`.
- S3 focused-test: `test_select_thread_rejects_bad_id`, `test_select_thread_unknown_thread`,
  `test_read_with_stale_selection_is_unknown_thread`, `test_list_threads_malformed`; MCP failure.
- S4 task-test-not-applicable for wording; `just docs-check`, `just cli-verbs-check`,
  `just mcp-spec-check`, `just coverage-check` and `tests/mcp/core/test_tool_docs.py` gate it.
