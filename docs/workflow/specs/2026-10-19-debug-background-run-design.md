# Let a debug session leave the guest running between calls

## Problem and authority

Issue #3244: `debug.interrupt` can never meet a running guest. Every gdb/MI resume
(`debug.continue`, `debug.advance`) waits for a stop and interrupts back on timeout under the
per-session lock (`ExecutionControl.resume`), so each call returns with the guest halted. Since
#3245 (PR #3268, ADR-0798) `debug.start_session` no longer probes, and the first real gdb
attachment (`-target-select remote`) is what halts a running guest. So `debug.interrupt` either
attaches (the attach halts the guest) or meets a guest an earlier call halted. It then sends
`-exec-interrupt`, waits ten seconds for a stop that cannot come, and answers `stopped` with no
reason. #3230 (PR #3250) made the interrupt read a stop carried by its own reply; it changes
nothing when there is no stop to read.

The issue offers two outcomes: a session can leave the guest running between calls, or the
tool's contract is redefined and the cells follow it. The choice is an interface decision for the
operator. This spec presents both, recommends the first, and designs it. The frozen issue
`WORK:SCOPE` owns the criteria and exclusions: the interrupt reply stop (#3230), the probe pause
(#3245), and durable cross-replica debug ownership (existing lifecycle architecture). Decision
record: [ADR-0783](../../adr/0783-debug-background-run.md).

## Options

| | A. Run between calls (recommended) | B. Redefine as "ensure halted" | B'. Remove `debug.interrupt` |
|---|---|---|---|
| Agent gets | resume without waiting, run any length, break in; guest runs while other tools drive it | an honest report of a halt it did not cause | nothing new; break-in only through the continue timeout |
| Size | about 600–800 changed lines, half tests | about 150–250 | about 200–300 (tool, exposure, generated verbs, cells, docs) |
| Risk | new between-call state; operations must refuse while running; detach of a running guest | low; the tool's only reachable effect is a no-op | low code risk; removes a published tool |
| Leaves | a waiting continue still blocks an interrupt for its wait | guest frozen between calls for the life of every gdbstub session | same frozen guest; long runs impossible (60 s cap per continue) |

B keeps the defect the issue names behind honest wording: no public operation can leave a
session's guest running, so an interrupt has nothing to interrupt. It also keeps a live
gdbstub session freezing the guest between calls. Any workload an agent drives over SSH or the
console stalls until the next blocking resume, and a hang that takes over 60 seconds to reproduce
can only be caught by repeated continue-timeout-interrupt cycles that each stop the kernel at an
arbitrary point. A fixes both at the cost of one engine-owned state and one refusal rule.
Rejected variants, recorded in ADR-0783: a lock-free interrupt that preempts a waiting continue,
and letting other operations silently consume a stop that arrived during a run.

## Design (option A)

### Execution state

`GdbMiAttachment` (`providers/ports/debug.py`) gains
`execution: Literal["attach_halted", "halted", "running"] = "halted"`. Only the engine writes it,
and only inside engine calls, which already run one at a time per session under the runtime's
per-session `asyncio.Lock` (`DebugEngineRuntime.lock_for`). `GdbMiEngine.attach` sets
`attach_halted`, because the RSP connect halts the guest (ADR-0798 evidence). Any operation that
reaches gdb other than `interrupt` moves `attach_halted` to `halted`. A non-waiting resume that
got `^running` and no stop sets `running`. Each observed `*stopped` sets `halted`.

### Engine and port surface

- `continue_background(attachment) -> GdbStopRecord | None` (new port method). In `halted` or
  `attach_halted`: `-exec-continue`, then return the redacted stop if the reply carries one
  (state `halted`), else `None` (state `running`). In `running`: send nothing, read the stream
  once (one `STOP_POLL_SLICE_SEC` slice) and return a stop if one arrived, else `None`.
- `continue_(attachment, timeout_sec)` keeps its contract. In `running` it sends no
  `-exec-continue` and waits for the run's stop, interrupting back with `timed_out=True` on
  timeout as today.
- `interrupt(attachment) -> GdbHalt`, replacing `GdbStopRecord | None`. `GdbHalt` is a new port
  model: `halted_by: Literal["interrupt", "stop_event", "attach", "already_halted"]` and
  `stop: GdbStopRecord | None`.
  - `running`: read the stream once. A stop found there → `stop_event` with that stop.
    Otherwise `-exec-interrupt`, take the stop from the reply (#3230) or wait the existing
    `INTERRUPT_STOP_TIMEOUT_SEC` → `interrupt` with that stop. No stop → raise
    `INFRASTRUCTURE_FAILURE` / `transport_stall`; state stays `running`.
  - `attach_halted` → `attach`, `stop=None`, state `halted`, no command sent.
  - `halted` → `already_halted`, `stop=None`, no command sent.
- `execute_mi_command` refuses in `running` before writing: `DEBUG_ATTACH_FAILURE` /
  `code="inferior_running"`, the existing code ADR-0275/0277/0278 already return for gdb's own
  running-target errors. Every gdb/MI command an operation sends goes through this function
  (breakpoints, watchpoints, registers, memory, symbols, stack, disassembly, modules and the
  resume verbs). The interrupt and the stream reads do not, so they stay reachable.
- The internal resume-timeout interrupt (`ExecutionControl.resume`) keeps its own private
  helper; it is not the port's `interrupt`.
- The fault-inject engine implements the same port: `continue_background` returns
  `None`, and `interrupt` returns `interrupt` with its existing `signal-received` stop.

### Tool contract

- `debug.continue` gains `wait: bool = True` (a `Field` description in the wrapper).
  `wait=true` is unchanged. `wait=false` returns `status="running"` with
  `suggested_next_actions=["debug.interrupt", "debug.continue"]`, or `stopped` with the stop's
  reason when the run had already stopped. The audit row records `wait`.
- `debug.interrupt` returns `status="stopped"` with `data.halted_by`, `data.timed_out=false`
  and `data.reason` only when a stop record was observed (`interrupt` → gdb's reason, normally
  `signal-received`; `stop_event` → for example `breakpoint-hit`). A stall is the failure above.
- Every other gdb/MI tool, `debug.advance` included, called while the guest runs returns
  `debug_attach_failure` with `data.code="inferior_running"` and
  `suggested_next_actions=["debug.interrupt", "debug.continue"]`.
- The wrapper docstrings of `debug.continue` and `debug.interrupt` state all of this,
  including that a stop ending a run is reported only by the next `debug.interrupt` or
  `debug.continue`, and that a waiting `debug.continue` holds the session until its stop or
  wait (at most 60 seconds), during which an interrupt waits.
  The "target must already be stopped" sentences on the read tools stay true and are left alone.

### Lock and concurrency model

The existing per-session lock remains the only serialization. No new lock or thread is
added. A non-waiting continue releases the lock when it returns. An interrupt takes the lock
like any operation, so it queues behind a waiting continue. State changes happen only inside
engine calls under that lock, so no two writers touch one attachment's state or gdb stream.
The engine's in-process registry stays process-scoped (ADR-0034 decision 1).

### Ending a session

`DebugEngineRuntime.reap` halts a `running` attachment with the engine `interrupt`
(best-effort, bounded by `INTERRUPT_STOP_TIMEOUT_SEC`) before `controller.exit()`. gdb then
exits from a halted target, the path the existing session cells prove resumes the guest.
`end_session` runs `reap` through `asyncio.to_thread` under the session lock, because the halt
can block for up to ten seconds; today's `exit()` already blocks briefly on the loop.

## Success

- C1: `debug.continue(wait=false)` returns `running` and the guest keeps executing after the
  call; the lock is free.
- C2: `debug.interrupt` on a running guest halts it and reports `halted_by="interrupt"` with
  gdb's reason; with a stop that already arrived, `stop_event` and its reason; on a halted
  guest, `attach` or `already_halted` with no gdb/MI command sent and no ten-second wait.
- C3: while the guest runs, the gdb/MI tools named under *Tool contract* refuse with
  `inferior_running` and send no gdb/MI command. `debug.continue(wait=true)` reports the run's
  stop without resuming again.
- C4: a running interrupt with no stop fails `transport_stall`, never a reasonless `stopped`.
- C5: `debug.end_session` on a running session halts before gdb exits, and the guest answers
  again afterwards.
- C6: the x86_64 local `debug.interrupt` cell follows the new contract and passes against a
  deployed candidate; wrapper text, generated docs and the runbook's known-gap bullet match.

## Failure model

1. Actors and deployments
   - authenticated contributor agents on local-libvirt; remote-libvirt shares the engine.
   - one server process owning a session's attachment, as today.
2. Invariants and assets
   - the reported stop is the one gdb reported; no reasonless or invented stop.
   - one writer per gdb/MI stream; per-session lock serialization.
   - guest execution after `end_session` (the existing detach-resumes contract).
   - the published tool contract (`debug.continue`, `debug.interrupt` envelopes).
3. Accepted failure classes
   - an interrupt waits behind a waiting continue for up to its wait (60 s at most): bounded
     and documented; `wait=false` is the interruptible form.
   - a server restart loses the state with the attachment; the next attachment halts the guest
     and reports `attach`: existing ADR-0034 decision 1 behavior.
   - a guest that stops at a breakpoint during an unobserved run stays halted until the
     agent's next `debug.interrupt`, `debug.continue` or `debug.end_session`, the same exposure
     as a guest halted between calls today.
   - a reap whose halt stalls exits gdb anyway and leaves guest state unknown: `end_session`
     has already committed the detach, and the preceding stall was reported to the agent.
   - a guest left running while its session is detached by the control plane: the existing
     force-crash/reboot detach does not reap engines today, and this design does not change it.
4. Covered elsewhere
   - interrupt reply stop: #3230 (merged). Probe pause: #3245 / ADR-0798 (merged).
   - cross-replica session ownership: existing lifecycle architecture (excluded).
   - ppc64le and remote-libvirt qualification: their own owners (#2818, #3113); x86 first.

No trust boundary is added or widened. Authorization, project gating, the audit trail and the
redaction of stop records are unchanged, so no threat model is required.

## Global Constraints

Python 3.14; existing dependencies only; preserve the x86_64 and ppc64le targets. No migration,
schema, lifecycle protocol, ADR index or persisted-data change. ADR-0783 stays Proposed until the
implementing PR merges; cite #3244, not the ADR, from `src/` and `tests/` until then.

## Validation

Engine, with the existing fake MI controller (`tests/providers/local_libvirt/test_debug_gdbmi.py`):

- `continue_background`: `^running` only → `None` and `running`; a reply carrying `*stopped` →
  that stop and `halted`; in `running`, no write and one read.
- In `running`, each gdb/MI command operation raises `inferior_running` with zero controller
  writes, parametrized over all of them. Controlled fault: remove the guard and observe red.
- `interrupt`: `running` with a queued stop → `stop_event`, no `-exec-interrupt`; `running`
  without one → `interrupt` with the reply's or the polled stop; no stop → `transport_stall`
  and still `running`; `attach_halted` → `attach`; `halted` → `already_halted`; both with zero
  writes and no wait (the injected sleep/clock shows no ten-second poll).
- `continue_` in `running`: no `-exec-continue`; stop reported; timeout interrupts back.

Tool layer (`tests/mcp/debug/test_debug_ops.py`): the `wait` schema field, the `running`
envelope and its next actions, the interrupt `halted_by`/`reason` data, the
`inferior_running` envelope through a real op, and the continue audit row's `wait`.

Session end (session tests under `tests/mcp/debug/`): a running attachment gets
`-exec-interrupt` before `exit()`, off the event loop; a halted one gets no interrupt.

Live cell unit tests (`tests/integration/live_stack/test_debug_cells.py`): the rewritten
interrupt body against a scripted session, including its failure messages.

The rewritten `debug.interrupt` cell: start; `debug.continue(wait=false)` answers `running`; the
guest answers SSH (runs between calls); interrupt → `halted_by="interrupt"`, `signal-received`;
the guest stops answering; `rip` is a kernel address. Then set the `vfs_read` breakpoint, continue
without waiting, write the SSH banner, interrupt → `stop_event` with `breakpoint-hit`; clear,
end, and the guest answers again.

Regenerate `docs/guide/reference/debug.md` and `src/kdive/cli/commands/_generated_verbs.py`
with their existing recipes. Gates: `just lint`, `just type`, focused tests, then the managed
pre-push `just ci`.

Live proof (needs separate operator admission): x86_64 local-libvirt, default configuration,
on a disposable Fedora 44 x86_64 KVM lab guest with the `longterm` fixture already built. The
candidate head is deployed through `examples/local-libvirt/demo-up.sh` with an on-disk server
`TMPDIR` (#3229). Run the functional cells of `debug.interrupt`, `debug.continue`,
`debug.advance`, `debug.end_session` and gdbstub `debug.start_session` from
`tests/integration/test_debug_tool_cells_live.py`, then `demo-down.sh --wipe --yes` and the
runbook's leftover-domain and staging checks. Estimated at 45–60 minutes with the fixture
present; a fixture build adds its own cost and needs its own approval. Offline green is not
that proof.
