# 0819 — A DebugSession holds an explicit gdbstub CPU selection that stack and register reads pass to gdb

## Status

Accepted (2026-11-17)

## Context

QEMU's gdbstub exposes each vCPU as a gdb thread. `debug.read_registers` and `debug.backtrace`
send MI commands with no thread argument, so they act on whichever thread gdb has current, and
ADR-0781 reads that thread implicitly for `debug.read_frame`. gdb makes the stopping thread
current at a stop, so a contributor debugging a multi-vCPU kernel cannot list the CPUs, choose
one, or keep reading the same one (#924).

## Decision

Add two gdb/MI debug tools on the engine port, both contributor and refused while the guest
runs like the other stack ops.

- `debug.list_threads` runs `-thread-info` and returns up to 128 threads (`id`, `target_id`,
  `details`, `state`, frame `func`/`addr`, plus `name` when gdb sends one), `truncated`, gdb's
  `current_thread_id` and kdive's `selected_thread_id` (null when none).
- `debug.select_thread(thread_id)` checks the id with `-thread-info N` and records it as the
  attachment's `selected_thread`. It returns the thread row. It sends no `-thread-select`, so
  gdb's own current thread is untouched. The tool is a state change and is audited.

While a thread is selected, `read_registers`, `backtrace` and `read_frame` add `--thread N` to
their MI commands; with none selected they behave as before. The selection lives on the
in-memory `GdbMiAttachment`, written only inside engine calls the runtime serializes per session,
so there is no schema change. It ends with the attachment (re-attach) and is shared by every
contributor on the session.

Failures: a non-positive or non-integer id is `CONFIGURATION_ERROR` / `bad_thread_id` before any
MI command; an id `-thread-info N` does not list, or a later command answered
`Invalid thread id: N`, is `DEBUG_ATTACH_FAILURE` / `unknown_thread` (the stale-id case, with the
selection kept so the client sees why); a reply without a thread list or with a row lacking a
numeric `id` is `malformed_thread_info`; an empty list from `-thread-info` is `no_threads`.

## Consequences

- A selected thread keeps answering after a stop moves gdb's current thread; a client reads
  `selected_thread_id` and `current_thread_id` from `debug.list_threads` to tell them apart.
- `read_registers` and `backtrace` results do not echo the thread; a second contributor can
  change the selection between calls.
- No selection can be cleared except by re-attaching.
- Rows carry the CPU number only in `details` (`CPU#1 [halted ]`); thread id 2 is CPU#1.
- The fault-inject engine reports one synthetic thread.

## Considered & rejected

- **Call `-thread-select N` and rely on gdb's current thread.** verified: gdb 17.2 (Fedora 44)
  against `qemu-system-x86_64 -smp 3 -S -gdb tcp::PORT` answers `-thread-info 2` with one row
  and `-thread-info 9` with `^done,threads=[]`; gdb makes the stopping thread current at a
  stop (gdb manual, all-stop mode), so the choice would silently change under the client.
- **Persist the selection in the DebugSession row.** judgment: needs a schema migration for state
  that cannot outlive the gdb process it names.
- **A `thread` parameter on every debug tool.** judgment: owned by no issue; operator-excluded
  from #924.
- **Leave the implicit current thread.** verified: `rg -n "thread-select|thread-info" src/` at
  4b4162a73 finds nothing, and ADR-0781's read of `-thread-list-ids` is the only thread use.
