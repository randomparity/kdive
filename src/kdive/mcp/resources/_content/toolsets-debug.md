# debug toolset

These drive a live GDB-based kernel debugging session against a booted system. Reach for
them to halt the kernel, inspect state, and step through code. Start a session, do the
inspection, then end it. For exact parameters, types, and return schema, read each tool's
own description.

**Provisioning prerequisite.** A live session needs the system to have been provisioned
with the profile's `debug` section `gdbstub: true`. This is bound at provision — you
cannot enable it on a ready system. If the system was not provisioned for it,
`debug.start_session` fails with a configuration error telling you to reprovision with
gdbstub set, and reprovisioning rebuilds and reboots the system. See the
provisioning-for-debugging notes in the investigation index and decide up front.

## Session lifecycle

- `debug.start_session` — attach a GDB session to a booted system's stub.
- `debug.get_session` — read the status of a debug session.
- `debug.list_sessions` — list the debug sessions you can see.
- `debug.end_session` — detach and end a session when done.

## Run control

- `debug.continue` — resume a halted kernel.
- `debug.interrupt` — halt a running kernel to inspect it.

## Breakpoints and watchpoints

- `debug.set_breakpoint` — set a breakpoint at a symbol or address.
- `debug.list_breakpoints` — list the current breakpoints.
- `debug.clear_breakpoint` — remove a breakpoint.
- `debug.set_watchpoint` — trap a write to a data address.
- `debug.list_watchpoints` — list the current watchpoints.
- `debug.clear_watchpoint` — remove a watchpoint.

## Inspecting state

- `debug.read_registers` — read the CPU registers at the halt.
- `debug.read_memory` — read kernel memory at an address.
- `debug.resolve_symbol` — resolve a symbol name to an address (or the reverse).
- `debug.backtrace` — unwind the call stack at the halt.
- `debug.read_frame` — select and read a single stack frame.
- `debug.disassemble` — disassemble instructions around an address.

## Modules

- `debug.list_modules` — list the loaded kernel modules and their base addresses.
- `debug.load_module_symbols` — load a module's debug symbols so its frames resolve.
