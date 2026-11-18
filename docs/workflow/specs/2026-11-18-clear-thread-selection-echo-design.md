# Clear the CPU selection and echo the read thread (#3691)

Decision: [ADR-0824](../../adr/0824-clear-the-cpu-selection-and-echo-the-read-thread.md),
amending [ADR-0819](../../adr/0819-gdbstub-thread-selection.md).

## Problem

A CPU selected with `debug.select_thread` cannot be undone short of re-attaching, and
`debug.read_registers` and `debug.backtrace` do not say which CPU they read.

## Design

Port (`providers/ports/debug.py`): `select_thread(attachment, *, thread_id: int | None) ->
GdbThread | None` (`None` in, `None` out); new `read_thread_id(attachment) -> str`.

Engine (`gdbmi/commands/threads.py`): `select_thread(None)` sets `attachment.selected_thread =
None`, sends no MI command, returns `None`. `read_thread_id` is the existing
`StackCommands._current_thread` moved here: the selected id, else `-thread-list-ids`'s
`current-thread-id`, else `no_current_thread`. `read_frame` calls it from its new home. Reads
already omit `--thread` when nothing is selected (`thread_option`), so no MI change.

Fault-inject: `select_thread(None)` clears; `read_thread_id` returns the selection or `"1"`.

MCP: `debug.select_thread` takes `thread_id: int | None = None`; `None` returns status `cleared`,
`data={"thread_id": null}`. `_read_registers_op` and `_backtrace_op` call
`engine.read_thread_id` after the read succeeds and add `thread_id` to `data` (flat map for
registers, a reserved key beside the register names). Docstrings and `Field` text name the clear value and the field and
drop "cannot be cleared"; the generated tool reference, CLI verb (`--thread-id` becomes optional;
omitting it clears), toolset docs and resource content follow.

## Failure model

1. Actors and deployments: unchanged from ADR-0819's spec (contributor on a live gdbstub session;
   local and remote libvirt engines; x86_64 and ppc64le).
2. Invariants: results of a session that never selected a thread differ only by `thread_id`; no
   schema change; clearing sends no MI command and, like every op, is refused while the guest runs; `bad_thread_id` still precedes any MI command.
3. Accepted: the selection is still shared per session and ends at re-attach; the id echoed names
   the CPU asked for, and a thread that vanished still fails as `unknown_thread` (registers) or
   by gdb's own error; one extra `-thread-list-ids` per unselected read.
4. Covered elsewhere: echo on `read_memory`/`read_frame`/`disassemble` and a per-call override
   are unowned exclusions; the live-VM proof is #3690.

## Success

- S1 `select_thread(null)` clears, reports `cleared`, and later reads omit `--thread`
  (criterion 1).
- S2 `read_registers` and `backtrace` carry `thread_id`: selected, else current (criteria 2, 4).
- S3 docstrings, reference, CLI verb, index and tests match (criterion 3).
- S4 live cells assert the clear path and the echo (criterion 6).

## Validation

- S1 focused-test: `tests/providers/local_libvirt/test_debug_gdbmi.py::test_select_thread_none_*`;
  MCP `tests/mcp/debug/test_debug_ops.py::test_select_thread_null_clears`.
- S2 focused-test: `test_read_thread_id_*`; MCP `test_read_registers_echoes_thread*`,
  `test_backtrace_echoes_thread*`.
- S3 `just docs-check`, `just cli-verbs-check`, `just mcp-spec-check`,
  `tests/mcp/core/test_tool_docs.py`.
- S4 `just test-live-stack` cells, run by #3690 after merge; locally only collected and linted.
