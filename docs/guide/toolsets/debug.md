# debug toolset

Use GDB to halt and inspect a live kernel. `debug.start_session` takes a **Run ID** and
`transport="gdbstub"`; subsequent operations take the returned **DebugSession ID**. Starting,
controlling, and ending a session require contributor access. Read each tool's schema for
its limits and returned fields.

Provision the System with `debug.gdbstub: true` before this workflow. Attach requires a
completed build, a successful boot result, and a bound System in READY or PAUSED state;
a build's `succeeded` status alone is insufficient. A declared console-only crash is not a
live-debug target. A `crashed_halted_live` boot can instead permit GDB attachment. Only one
session per transport can occupy a System at a time.

If GDB support was omitted from the profile, reprovisioning a READY System with a changed
profile rebuilds the guest; preserve needed evidence first. See
resource://kdive/docs/guide/toolsets/systems.md for provision and snapshot constraints.

## Session lifecycle

- `debug.start_session` — attach a GDB session to a booted system's stub.
- `debug.get_session` — read the status of a debug session.
- `debug.list_sessions` — list the debug sessions you can see.
- `debug.end_session` — detach and end a session when done.

## Run control

- `debug.continue` — resume a halted kernel and wait for a stop event.
- `debug.interrupt` — halt a running kernel to inspect it.
- `debug.advance` — advance a stopped kernel by one step; `mode` picks the unit:
  - `into` — one source line, into called functions.
  - `over` — one source line, over called functions.
  - `instruction` — one machine instruction (works without debug symbols).
  - `out` — resume until the current function returns; it needs a frame that can return.

Inspect the returned stop reason and `data.timed_out` before assuming the intended stop
was reached. End the session with `debug.end_session` when inspection is complete.

## Breakpoints and watchpoints

- `debug.set_breakpoint` — set a breakpoint at a symbol (a bare C function or variable name; not an address).
- `debug.list_breakpoints` — list the current breakpoints.
- `debug.clear_breakpoint` — remove a breakpoint.
- `debug.set_watchpoint` — trap a write to a data address.
- `debug.list_watchpoints` — list the current watchpoints.
- `debug.clear_watchpoint` — remove a watchpoint.

## Inspecting state

- `debug.read_registers` — read the CPU registers at the halt.
- `debug.read_memory` — read kernel memory at an address.
- `debug.resolve_symbol` — resolve a symbol name to an address.
- `debug.backtrace` — unwind the call stack at the halt.
- `debug.read_frame` — select and read a single stack frame.
- `debug.disassemble` — disassemble instructions around an address.

`debug.resolve_symbol` yields an address; it does not evaluate member, array, or type-aware
expressions. For typed reads such as a struct field, use `introspect.script` on a separate
`drgn-live` session. That path requires a running guest and its own transport/debug-info
prerequisites: resource://kdive/docs/guide/toolsets/introspect.md.

## Modules

- `debug.list_modules` — list the loaded kernel modules and their base addresses.
- `debug.load_module_symbols` — load a module's debug symbols so its frames resolve.
