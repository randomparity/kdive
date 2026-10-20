# 0783 — A debug session can leave the guest running between calls

## Status

Accepted (2026-10-19)

Implements the operator-approved option A for issue #3244. Qualifies ADR-0034 decision 5: the
tools stay synchronous and jobless, but a call may now return with the guest still running.
Other ADR-0034, ADR-0216, ADR-0463 and ADR-0798 decisions remain in effect.

## Context

Every gdb/MI resume (`debug.continue`, `debug.advance`) waits for a stop and interrupts back on
timeout, all under the per-session lock, so each call returns with the guest halted. Since
ADR-0798 the first real gdb attachment is what halts a running guest. `debug.interrupt`
therefore only ever meets a halted guest: it sends `-exec-interrupt`, waits ten seconds for a
stop that cannot come and answers `stopped` with no reason. Between calls a live gdbstub
session also freezes the guest, so a workload driven over SSH or the console stalls until the
next blocking resume.

## Decision

`debug.continue` gains `wait` (default `true`, today's behavior). `wait=false` resumes and
returns `running` without waiting; the guest keeps running after the call and the per-session
lock is released. The engine owns one execution state per attachment — `attach_halted`,
`halted` or `running` — written only inside engine calls under the per-session lock. A resume
sets `running` before it is written; only an observed stop sets `halted`, and a resume gdb
refuses restores it. So `running` means "not known to be halted".

While `running`, the runtime refuses every operation except `debug.continue` and
`debug.interrupt` before any gdb/MI command, with `debug_attach_failure` / `inferior_running`.
`debug.interrupt` reports `halted_by`, derived from the stop itself: `interrupt` for its own
`signal-received` stop with signal `SIGINT`, `stop_event` for any other stop (a breakpoint
during the run), `attach` when the first gdb attachment halted the guest, and `already_halted`
when nothing ran (no command sent). A running interrupt with no stop fails
`infrastructure_failure` / `transport_stall`, one gdb refuses fails `debug_attach_failure` /
`interrupt_refused` at once, and in both cases the state stays `running`.
`debug.continue(wait=true)` on a running guest waits for its stop without resuming again. Ending
a running session halts it before gdb exits, keeping the detach path the session cells prove,
and reports `guest_execution="unknown"` when that halt stalls or gdb refuses it. On request
cancellation the lock holder keeps awaiting the shielded engine call until it finishes before
re-raising, so the lock is never freed under a running thread; an op re-checks that its session
is live after taking the lock.

## Consequences

An agent can let the kernel run for as long as it likes, drive a workload with other tools,
then break in. A stop that ends a run is reported by the next `debug.interrupt` or
`debug.continue`, never by other operations, which refuse until then. A waiting
`debug.continue` still holds the lock, so an interrupt queues behind it for its wait plus the
interrupt-back (about 90 seconds at most). A cancelled request now keeps the session busy until
its engine call returns. The state is process-local like the attachment it sits on: a server
restart loses it and the next attachment halts the guest again. No schema, migration or
lifecycle protocol changes.

## Considered & rejected

- **Redefine `debug.interrupt` as "ensure halted" and keep every call synchronous.** judgment:
  makes the tool's only reachable effect a no-op report, and leaves the guest frozen between
  calls for the life of every gdbstub session.
- **Remove `debug.interrupt`.** judgment: the continue timeout already breaks in after at most
  60 seconds, but removal gives up long runs and the frozen-guest problem remains.
- **Let an interrupt preempt a waiting continue without the lock.** judgment: needs a second
  writer on one gdb/MI stream and a hand-off of the stop between two calls, for a case the
  non-waiting continue already covers.
- **Let other operations collect a stop that arrived during a run and proceed.** judgment:
  the stop's reason would reach no caller, and every operation would pay a stream read.
- **Guard inside `execute_mi_command`.** verified: `commands/modules.py` at `bb85e0eba` maps a
  command error without gdb's own "running" text to `module_decode_failed`
  (`_module_walk_error`) or swallows it (`_raise_unless_decode_failure`), so the module tools
  would misreport the refusal; the guard sits at op dispatch instead.
