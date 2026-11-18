# 0824 — `debug.select_thread` can clear the CPU selection, and register and backtrace reads echo their thread

## Status

Accepted (2026-11-18)

Amends [ADR-0819](0819-gdbstub-thread-selection.md): replaces two of its Consequences, that
results do not echo the thread and that a selection cannot be cleared. Its Decision, failure
codes and the rest of its Consequences stand.

## Context

ADR-0819 accepted two gaps. An agent that selected a CPU can return to gdb's implicit current
thread only by ending the session, and `debug.read_registers` and `debug.backtrace` do not say
which CPU a read came from. The selection is shared by every contributor on the session, so it can
change between calls, and confirming a read's CPU costs a `debug.list_threads` call that may
itself race another contributor (#3691).

## Decision

**Clear.** `debug.select_thread` takes `thread_id: int | None`. `null` sets the attachment's
`selected_thread` to `None` and sends no MI command, so no gdb state can fail it. Like every
other op it is refused while the guest runs. The result has status `cleared` and `data={"thread_id": null}`. Later reads
omit `--thread`, as they do before any selection, and `debug.list_threads` omits
`selected_thread_id`. Clearing with nothing selected succeeds and reports the same result. A
positive integer behaves as in ADR-0819; every other value keeps `bad_thread_id`. The call is
audited either way.

**Echo.** `debug.read_registers` and `debug.backtrace` add `thread_id`, a string like the ids in
`debug.list_threads`, to `data`: the selected thread, else gdb's current thread. The engine port
gains `read_thread_id(attachment)`, which returns `selected_thread` or asks gdb with
`-thread-list-ids` (`no_current_thread` when gdb reports none; this is the lookup
`debug.read_frame` already performs, moved to the shared command class). The operation resolves
it before the read, so the id names the CPU the read was sent to; with a selection it costs no MI
command.

**`read_registers` shape.** `data` stays a flat map of register name to hexadecimal string, with
`thread_id` as one reserved extra key. A register request whose name is `thread_id` cannot
collide: gdb has no such register, so it already fails with `missing_registers` before a result
is built. The alternative, nesting registers under a `registers` key, would break every consumer
that reads `data["rip"]` for no benefit this issue asks for.

Only the two named tools echo. `read_memory`, `read_frame` and `disassemble` are unchanged.

## Consequences

- An agent returns to gdb's implicit current thread with `thread_id: null` and confirms the CPU of
  every register and backtrace read from the result itself.
- A consumer that iterates every key of a `read_registers` result sees one extra key,
  `thread_id`, whose value is a decimal string rather than a register value. Consumers that look
  registers up by name are unaffected. Results of sessions that never select a thread differ
  only by this field.
- Without a selection, each of the two reads costs one extra `-thread-list-ids`.
- A selection is still shared by every contributor and still ends at re-attach, and a
  contributor can now end it earlier.
- The fault-inject engine echoes its one synthetic thread, `1`.

## Considered & rejected

- **Nest registers under a key to leave room for metadata.** judgment: a breaking shape change
  for existing consumers to avoid a key that cannot collide with a register name.
- **Echo `selected_thread_id` only, null when nothing is selected.** judgment: the issue asks for
  the thread the read used; a null leaves the common no-selection case unanswered.
- **A separate clear tool.** judgment: one more tool surface for what one nullable parameter
  states, and the audit and exposure rows already exist for `debug.select_thread`.
- **Echo on `read_memory`, `read_frame` and `disassemble` too.** judgment: owned by no issue;
  operator-excluded from #3691.
- **A per-call thread override.** judgment: owned by no issue; operator-excluded from #3691.
