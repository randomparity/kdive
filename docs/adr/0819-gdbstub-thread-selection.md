# 0819 — gdbstub CPU selection is gdb's own current thread, set by `debug.select_thread`

## Status

Proposed

## Context

QEMU's gdbstub exposes each vCPU as a gdb thread. `debug.read_registers` and `debug.backtrace`
send MI commands with no thread argument, so they act on whichever thread gdb has current, and
ADR-0781 reads that thread implicitly for `debug.read_frame`. A contributor debugging a
multi-vCPU kernel has no way to list the CPUs or to choose one (#924). The persistent
`gdb --interpreter=mi3` controller of a DebugSession already holds a current thread.

## Decision

Add two gdb/MI debug tools on the existing engine port, both contributor, both refused while the
guest runs like the other stack ops.

- `debug.list_threads` runs `-thread-info` and returns every thread (`id`, `target_id`, `name`,
  `state`, frame `func`/`addr`) plus `current_thread_id`.
- `debug.select_thread(thread_id)` runs `-thread-select N` and returns the selected `thread_id`
  and its frame. `N` is a positive integer, validated before any MI command.

The selection is gdb's own current thread. `read_registers`, `backtrace` and `read_frame` keep
their signatures and read it as they do today. Nothing is stored in the session row, so there
is no schema change. gdb makes the stopping thread current at every stop, so a selection lasts
until the next stop or until the session is re-attached.

Failures: a non-positive or non-integer id is `CONFIGURATION_ERROR` / `bad_thread_id`; gdb's
`Thread ID N not known.` is `DEBUG_ATTACH_FAILURE` / `unknown_thread` (also the stale-id case); a
`-thread-info` reply with no thread list or a thread without a numeric id is
`DEBUG_ATTACH_FAILURE` / `malformed_thread_info`; an empty list is `no_threads`; a
`-thread-select` reply that omits or contradicts `new-thread-id` is `malformed_thread_select`.

## Consequences

- A client that needs a CPU after `debug.continue` or a step selects it again; the tool
  descriptions say so (gdb manual, all-stop mode). No live run proves it in this change.
- `debug.read_registers` and `debug.backtrace` responses do not echo the thread; the client
  reads it from `debug.select_thread` or `debug.list_threads`.
- gdb 17.2 (Fedora 44) answers `-thread-select 9` with no target as
  `^error,msg="Thread ID 9 not known."`; the unknown-thread mapping matches that text.
- Two tools are added to the MCP surface, the CLI verbs and the coverage obligations; the
  fault-inject engine reports one synthetic thread.

## Considered & rejected

- **Persist the selection in the DebugSession row.** judgment: needs a schema migration and a
  second copy of state that gdb overwrites at every stop, so the row would name a thread gdb is
  no longer using.
- **A `thread` parameter on every debug tool.** judgment: owned by no issue; operator-excluded
  from #924, and it widens eight tool schemas for a need one selector meets.
- **Re-send `-thread-select` before each read from a cached id.** judgment: hides the stop-time
  reselection from the client and adds a hidden command to every read.
- **Leave the implicit current thread.** verified: `rg -n "thread-select|thread-info"
  src/` at 4b4162a73 finds no match, so no tool lists or selects a CPU (#924).
