# 0783 — A debug session can leave the guest running between calls

## Status

Proposed (2026-10-19)

Proposed for issue #3244. Qualifies ADR-0034 decision 5: the tools stay synchronous and
jobless, but a call may now return with the guest still running. Other ADR-0034, ADR-0216,
ADR-0463 and ADR-0798 decisions remain in effect.

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
`halted` or `running` — changed only by engine calls made under the existing per-session lock.

While `running`, every gdb/MI command other than the stop-reading paths is refused before it
is sent, with `debug_attach_failure` / `inferior_running`. `debug.interrupt` reports how the
guest became halted in a `halted_by` field: `interrupt` (it halted a running guest), `stop_event`
(a stop that arrived during the run, such as a breakpoint, is reported now), `attach` (the first
gdb attachment halted it) or `already_halted` (nothing ran; no command is sent). A running
interrupt that yields no stop fails with `infrastructure_failure` / `transport_stall` and the state
stays `running`. `debug.continue` with `wait=true` on a running guest waits for its stop without
resuming again. Ending a running session halts it before the engine exits, so detach keeps
today's proven path.

## Consequences

An agent can let the kernel run for as long as it likes, drive a workload with other tools,
then break in. A stop that ends a run is reported by the next `debug.interrupt` or
`debug.continue`, never by other operations, which refuse until then. A waiting
`debug.continue` still holds the lock, so an interrupt queues behind it for the wait it asked
for (60 seconds at most). The state is process-local like the attachment it sits on: a
server restart loses it and the next attachment halts the guest again. No schema, migration or
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
