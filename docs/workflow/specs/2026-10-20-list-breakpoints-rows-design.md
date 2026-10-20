# debug.list_breakpoints rows (#3242)

Decision: [ADR-0780](../../adr/0780-list-breakpoints-rows.md).

## Problem

`debug.list_breakpoints` answers `data={"count": n}`. A client cannot see a breakpoint's number,
kind or location, so cannot pick one to clear, and the #2813 `debug.list_breakpoints` functional
cells fail on that gap after their count proof.

## Design

`_list_breakpoints_op` (`src/kdive/mcp/tools/debug/operations/breakpoints.py`) answers
`data={"count": len(rows), "breakpoints": rows}`, each row
`ref.model_dump(mode="json", exclude_none=True)` of the engine's `GdbBreakpointRef`, in engine
order — the shape `_list_watchpoints_op` already uses. Example:

    {"count": 2, "breakpoints": [
      {"number": "1", "type": "breakpoint", "addr": "0xffffffff8143f2a4", "func": "vfs_read"},
      {"number": "2", "type": "hw watchpoint", "what": "*(char(*)[8])0xffffffff82a05900"}]}

Status, next actions, auth, audit and errors are unchanged. The tool docstring names the row
fields; the generated reference and CLI verbs are regenerated, and the two toolset pages'
one-line summary says what is listed.

The live cell `list_breakpoints_body` sets `vfs_read` and `vfs_write`, then requires exactly
two rows: each set's returned `number`, `type` `breakpoint`, its `func`, and an `addr` within
that function by `/proc/kallsyms`. After clearing the first, exactly the `vfs_write` row
remains. The known-gap assertion is removed; the runbook's gap line drops the tool.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a live gdbstub DebugSession,
   local-libvirt and remote-libvirt engines; x86_64 cells, ppc64le shares the code path.
2. Invariants: the published `debug.list_breakpoints` result (additive `breakpoints`; `count`
   unchanged); redaction already applied by the engine projection.
3. Accepted: a multi-location breakpoint lists `addr` `<MULTIPLE>` without `func` (ADR-0780);
   rows are unbounded beyond what the caller installed, as for `debug.list_watchpoints`; the
   fake reports `enabled`, the real engine does not.
4. Covered elsewhere: frame arguments/locals #3243; interrupt behaviour #3230/#3244; ppc64le
   live cells #2818.

## Success

- S1 the answer lists every `-break-list` row the engine returns, with `number`, `type` and
  the location fields gdb reported (C1).
- S2 the four `debug.list_breakpoints` gdbstub functional cells (two configurations × two
  exposures) record `success` on a live x86_64 run (C2; live-gated).
- S3 tool reference and CLI verbs match the docstring (`just docs-check`, `just cli-verbs-check`).

## Validation

- S1 focused-test: `tests/mcp/debug/test_debug_ops.py::test_list_breakpoints_lists_rows`; red
  `KeyError: 'breakpoints'`.
- S2 focused-test: `tests/integration/live_stack/test_debug_cells.py` list-breakpoints cases;
  red until the body checks rows; live proof per the plan.
- S3 task-test-not-applicable for wording; the generators' check recipes gate the output.
