# 0780 — debug.list_breakpoints lists its rows

## Status

Proposed

## Context

`debug.list_breakpoints` answers only `count`, so a client cannot see which breakpoint to clear
(#3242). The shared engine's `list_breakpoints` already projects every gdb `-break-list` row,
watchpoints included, to a redacted `GdbBreakpointRef` filling `number`, `type`, `addr`,
`func` and `what`; the MCP op discards everything but the length. `debug.list_watchpoints`
(ADR-0277) already answers `count` plus its rows.

## Decision

`debug.list_breakpoints` answers `count` plus `breakpoints`: one object per engine row, the
ref's JSON dump without null fields, in gdb's order. `type` is the kind; the location is `func`
and `addr` for a breakpoint and `what` (the watched expression) for a watchpoint. `count` keeps
its meaning: the number of rows. The engine, its row set and its projection are unchanged.

## Consequences

- A watchpoint appears in both list tools, distinguished here by `type`.
- A breakpoint gdb resolved to several locations shows `addr` `<MULTIPLE>` and no `func`; its
  per-location detail is not listed.
- The real engine never reports `enabled` for a breakpoint; the fault-inject fake does.

## Considered & rejected

- **Narrow the list to breakpoints, as `list_watchpoints` narrows to watchpoints.** judgment:
  changes `count` for any session holding a watchpoint and moves the change into the shared
  gdb/MI command layer outside the approved surface, for a split `type` already gives.
- **Add the original location and per-location rows to the port model.** judgment: widens
  the provider port and both engines for a case the cells' single-location targets
  (`vfs_read`, `vfs_write`) do not exercise.
- **Leave the count-only answer.** verified: `list_breakpoints_body` in
  `tests/integration/live_stack/debug_cells.py` at bb5e6c41e asserts `breakpoints` in the answer
  and fails on its absence (#2813).
