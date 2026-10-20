# 0781 — debug.read_frame returns the frame's arguments and locals

## Status

Proposed

## Context

`debug.read_frame` answers only the frame's location, from `-stack-list-frames L L`, because
ADR-0275 decided "no `-stack-select-frame` session state, no frame-local values". A client
therefore cannot read the arguments or locals of the frame it selected, and the #2813
`debug.read_frame` functional cells fail on that gap (#3243). ADR-0275's two grounds no
longer hold for `-stack-list-variables`: it takes no caller expression, so the expression
surface ADR-0034 excluded stays closed, and the whole frame record can be redacted in one pass.

## Decision

Amend ADR-0275's "no frame-local values". `GdbMiEngine.read_frame` keeps its first command
and its errors, then asks gdb for the current thread (`-thread-list-ids`) and the frame's
variables with `-stack-list-variables --thread T --frame L --simple-values`. It returns a
`GdbFrameDetail`: the frame fields plus `args` and `locals` (gdb's `arg="1"` split, gdb's order),
each a `GdbVariable` of `name`, `type` and `value`, plus `truncated`. `--simple-values` gives a
value only for scalars and pointers; a struct, union or array variable carries its type alone.

The answer is bounded after redaction: at most 64 variables, arguments first, and each `type`
and `value` cut at 256 characters; `truncated` says whether anything was cut. The MCP result is
`data={level, frame:{...location, args, locals}, truncated}`. No session state changes: gdb
restores the selected frame after a `--frame` command.

## Consequences

- A `char *` value includes gdb's string preview of guest memory, which a contributor can
  already read verbatim with `debug.read_memory`. Redaction of registered secrets in it is best
  effort: gdb cuts the preview at 200 elements and compresses repeats before kdive redacts.
- An `<optimized out>` variable is reported as gdb prints it, not hidden.
- `read_frame` issues three MI commands instead of one; a missing current thread answers
  `no_current_thread` and a missing variable list `no_frame_variables`, both
  `DEBUG_ATTACH_FAILURE`.
- The fault-inject engine returns one `fmt` argument for its `panic` frame and no locals.
- `debug.backtrace` and stop records keep the location-only `GdbFrame`.

## Considered & rejected

- **`--frame` without `--thread`.** verified: gdb 17.2 (Fedora 44) answers
  `^error,msg="Cannot specify --frame without --thread"` to `-stack-list-variables --frame 1
  --simple-values` at a stop.
- **`-stack-select-frame` then `-stack-list-locals`.** judgment: leaves the selected frame
  changed for the next `debug.read_registers`, the session state ADR-0275 excluded; the
  `--thread T --frame L` form left `-stack-info-frame` at level 0 in the same gdb 17.2 probe.
- **`--all-values`.** verified: the same probe printed a by-value 300-byte struct argument in
  full (`{a = 'A' <repeats 299 times>, n = 1094795585}`); kernel structs make that the common
  case, and the 256-character cut would leave most of it unreadable.
- **A separate `debug.read_variables` tool or an opt-in parameter.** judgment: more surface for
  what the issue asks `read_frame` itself to return.
- **Leave the location-only answer.** verified: `read_frame_body` in
  `tests/integration/live_stack/debug_cells.py` at 010acfde5 asserts `args` or `locals` in the
  frame and fails on their absence (#2813).
