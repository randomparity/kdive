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
| Size | about 700–900 changed lines, half tests | about 150–250 | about 200–300 (tool, exposure, generated verbs, cells, docs) |
| Risk | new between-call state; operations must refuse while running; detach of a running guest | low; the tool's only reachable effect is a no-op | low code risk; removes a published tool |
| Leaves | a waiting continue still blocks an interrupt for its wait | guest frozen between calls for the life of every gdbstub session | same frozen guest; long runs impossible (60 s cap per continue) |

B keeps the defect the issue names behind honest wording: no public operation can leave a
session's guest running, so an interrupt has nothing to interrupt. It also keeps a live
gdbstub session freezing the guest between calls. Any workload an agent drives over SSH or the
console stalls until the next blocking resume, and a hang that takes over 60 seconds to reproduce
can only be caught by repeated continue-timeout-interrupt cycles that each stop the kernel at an
arbitrary point. A fixes both at the cost of one engine-owned state, one refusal guard and
two lock fixes.
Rejected variants, recorded in ADR-0783: a lock-free interrupt that preempts a waiting continue,
and letting other operations silently consume a stop that arrived during a run.

## Design (option A)

### Execution state

`GdbMiAttachment` (`providers/ports/debug.py`) gains
`execution: Literal["attach_halted", "halted", "running"] = "halted"`. Only the engine and the
runtime guard read it, and only the engine writes it. Both run inside engine calls, which run
one at a time per session under the runtime's per-session lock (see *Lock and concurrency
model*). The transitions are:

- `GdbMiEngine.attach` sets `attach_halted`, because the RSP connect halts a running guest
  (ADR-0798 evidence).
- Before writing any resume verb (`-exec-continue`, `-exec-step`, `-exec-next`,
  `-exec-step-instruction`, `-exec-finish`), the engine sets `running`. That covers the waiting
  continue, the non-waiting continue and every `debug.advance` mode.
- A resume whose reply is `^error` sets the state back to `halted`, since gdb refused to resume.
  Examples are a watchpoint that cannot be inserted, or "Cannot find bounds of current function".
- Each observed `*stopped` sets `halted`. That includes a stop in a resume's reply, its wait, its
  interrupt-back, or a `debug.interrupt`.
- Every other outcome after a resume or interrupt was written leaves `running`. That includes a
  write timeout, a stalled interrupt-back (`transport_stall`) and an interrupt with no stop.
  `running` therefore means "not known to be halted", and the next `debug.interrupt` sends
  `-exec-interrupt` again rather than claiming a halt.
- Any other operation that reaches gdb moves `attach_halted` to `halted`.

### Engine and port surface

- `continue_background(attachment) -> GdbStopRecord | None` (new port method). In `halted` or
  `attach_halted`, it writes `-exec-continue` and returns the redacted stop if the reply carries
  one (state `halted`), else `None` (state `running`). In `running` it sends nothing and returns
  `None`. It is a separate method rather than a `wait` flag on `continue_`, so that
  `continue_` keeps its non-optional `GdbStopRecord` return.
- `continue_(attachment, timeout_sec)` keeps its contract. In `running` it sends no
  `-exec-continue`, waits for the run's stop, and interrupts back with `timed_out=True` on
  timeout as today.
- `interrupt(attachment) -> GdbHalt`, replacing `GdbStopRecord | None`. `GdbHalt` is a new port
  model with `halted_by: Literal["interrupt", "stop_event", "attach", "already_halted"]` and
  `stop: GdbStopRecord | None`.
  - In `running`, it writes `-exec-interrupt`, then takes the stop from the reply (#3230) or
    waits the existing `INTERRUPT_STOP_TIMEOUT_SEC`. `halted_by` comes from that stop, not from
    which read found it: reason `signal-received` with `signal-name` `SIGINT` → `interrupt`;
    any other or absent reason or signal → `stop_event`. `GdbStopRecord` gains
    `signal_name: str | None`, parsed by `stop_record_from` from gdb's `signal-name`, so a stray
    signal stop (for example a `SIGTRAP` on another vCPU) is never reported as the agent's own
    interrupt. A breakpoint that fires during the run, even between the write and the reply,
    is therefore reported as `stop_event` with `breakpoint-hit`. If no stop arrives, it raises
    `INFRASTRUCTURE_FAILURE` / `transport_stall` and the state stays `running`.
  - In `attach_halted`, it returns `attach` with `stop=None`, sends no command and sets the state
    to `halted`.
  - In `halted`, it returns `already_halted` with `stop=None` and sends no command.
- The internal interrupt-back in `ExecutionControl.resume` stays a private helper. It is not
  the port's `interrupt`.
- The fault-inject engine keeps no state and never claims `running`. Its `continue_background`
  returns its existing synthetic `breakpoint-hit` stop, and its `interrupt` returns
  `already_halted` with no stop.

### Runtime guard

`_attach_and_run` (`mcp/tools/debug/operations/runtime.py`) refuses an operation after
`attach_or_reuse` and before calling it when the attachment is `running`. The refusal is
`DEBUG_ATTACH_FAILURE` with `data.code="inferior_running"` and
`suggested_next_actions=["debug.interrupt", "debug.continue"]`. Only the `debug.continue` and
`debug.interrupt` ops pass through, marked by a `while_running=True` argument on
`run_engine_op_with_resolver`. The guard sits at op dispatch rather than in
`execute_mi_command`. The module walk wraps or swallows command errors
(`commands/modules.py` `_module_walk_error`, `_raise_unless_decode_failure`), so a guard inside
the command path would surface as `module_decode_failed` or silence. At dispatch, every tool
refuses the same way and sends no gdb/MI command. Those tools are `debug.advance`, breakpoints,
watchpoints, memory, registers, symbols, stack, disassembly and modules.

`_op_failure`, which passes no next actions today, maps the error's `details["code"]` to them:
`inferior_running` → `["debug.interrupt", "debug.continue"]`, `transport_stall` →
`["debug.interrupt", "debug.end_session"]`, `interrupt_refused` → `["debug.end_session"]`; every
other code keeps today's empty list. `interrupt_refused` is an interrupt (the tool's or a resume's
interrupt-back) that gdb answers with `^error` and no stop: it fails at once as
`debug_attach_failure` instead of waiting out the stop bound, and the state stays `running`.

### Tool contract

- `debug.continue` gains `wait: bool = True`, described by a `Field` in the wrapper.
  `wait=true` is unchanged. `wait=false` ignores `timeout_sec` and returns `status="running"`
  with `suggested_next_actions=["debug.interrupt", "debug.continue"]`. If the resume reply
  already carried a stop, it returns `stopped` with that stop's reason. On a guest that is
  already running, `wait=false` returns `running` again and sends nothing. The audit row
  records `wait`.
- `debug.interrupt` returns `status="stopped"` with `data.halted_by`, `data.timed_out=false`,
  and `data.reason` only when a stop record was observed. A stall fails `transport_stall` with
  `suggested_next_actions=["debug.interrupt", "debug.end_session"]`. Each attempt is bounded by
  the 10-second command write plus the 10-second stop wait.
- Every other gdb/MI tool returns the runtime guard's `inferior_running` refusal while the guest
  runs.
- `debug.end_session` returns `data.guest_execution="unknown"` only when the session's guest
  was running and the halt before detach stalled or was refused (see *Ending a session*). Otherwise its reply is
  unchanged.
- Wrapper docstrings carry the agent-facing contract:
  - `debug.continue` and `debug.interrupt` state all of the above.
  - A stop that ends a run is reported only by the next `debug.interrupt` or
    `debug.continue(wait=true)`.
  - A waiting `debug.continue` holds the session until its stop, or until its wait plus the
    interrupt-back, which is about 90 seconds at most. An interrupt waits during that time.
  - `debug.end_session` states that ending a running session first halts it, adding up to about
    20 seconds.
  - The read tools' existing "target must already be stopped" sentences stay true and are left
    alone.

### Lock and concurrency model

The per-session `asyncio.Lock` remains the only serialization, and no new lock is added. Two
existing gaps are closed because the execution state depends on them:

- **Cancellation.** Inside the lock, the `asyncio.to_thread` engine call runs as a task. The
  lock holder awaits it through `asyncio.wait`, which shields the task from the caller's
  cancellation; on `CancelledError` it keeps awaiting the same task, in a loop so a repeated
  cancel cannot escape, until the task is done, and only then re-raises the cancellation. A plain `shield` is not enough: the cancelled coroutine would still
  leave the `async with` block and free the lock while the thread drives gdb. With the loop, a
  cancelled MCP request (client timeout or cancel notification) keeps the lock until the thread
  finishes, so one gdb/MI stream never has two writers. `end_session`'s `to_thread(reap)` runs
  under the same helper.
- **Ended session.** After taking the lock, `_run_engine_op` re-reads the session's `live` state
  before `attach_or_reuse`. An op queued behind `end_session`'s reap returns the existing
  not-live refusal instead of attaching a new gdb to a detached session.

A non-waiting continue releases the lock when it returns. The engine's registry stays
process-scoped (ADR-0034 decision 1).

### Ending a session

`DebugEngineRuntime.reap` halts a `running` attachment with the engine `interrupt` before
`controller.exit()`. That is best-effort and bounded by about 20 seconds, so gdb exits from a
halted target. Exiting from a halted target is the path the existing `debug.end_session` cell
proves resumes the guest. `reap` returns `False` only when it had to halt a `running`
attachment and that halt stalled or was refused; a never-attached session, a halted attachment and a drgn-live
session return `True`.
`end_session` runs `reap` through `asyncio.to_thread` under the session lock. When the halt
stalled, it adds `data.guest_execution="unknown"` to the detached reply.

## Success

- C1: `debug.continue(wait=false)` returns `running`, the guest keeps executing after the call,
  and the session lock is free.
- C2: `debug.interrupt` on a running guest halts it. It reports `halted_by="interrupt"` with
  `signal-received` when its interrupt caused the halt, and `stop_event` with the reason for any
  other stop. On a halted guest it reports `attach` or `already_halted`, with no gdb/MI command
  sent and no wait.
- C3: while the guest runs, the tools named under *Runtime guard* refuse with `inferior_running`
  and send no gdb/MI command. `debug.continue(wait=true)` reports the run's stop without resuming
  again.
- C4: after any resume or interrupt failure that leaves the guest's state unknown, the state is
  `running`. A running interrupt that gets no stop fails `transport_stall`; it never returns a
  reasonless `stopped` or `already_halted`.
- C5: `debug.end_session` on a running session halts the guest before gdb exits, and the guest
  answers again afterwards. A stalled halt is reported as `guest_execution="unknown"`.
- C6: a cancelled request keeps the lock until its engine call ends, and an op queued behind an
  ended session attaches nothing.
- C7: the x86_64 local `debug.interrupt` cell follows the new contract and passes against a
  deployed candidate. The wrapper text, the generated docs and the runbook's known-gap bullet
  match.

## Failure model

1. Actors and deployments
   - authenticated contributor agents on local-libvirt; remote-libvirt shares the engine.
   - one server process owning a session's attachment, as today.
2. Invariants and assets
   - the reported halt cause and stop are the ones gdb reported; no reasonless or invented stop.
   - one writer per gdb/MI stream; per-session lock serialization, including under cancellation.
   - guest execution after `end_session` (the existing detach-resumes contract).
   - the published tool contract (`debug.continue`, `debug.interrupt`, `debug.end_session`).
3. Accepted failure classes
   - an interrupt waits behind a waiting continue for up to about 90 s. That is bounded and
     documented, and `wait=false` is the interruptible form.
   - a server restart loses the state along with the attachment. The next attachment halts the
     guest and reports `attach`, which is the existing ADR-0034 decision 1 behavior.
   - a guest that stops at a breakpoint during an unobserved run stays halted until the agent's
     next `debug.interrupt`, `debug.continue` or `debug.end_session`. That is the same exposure
     as a guest halted between calls today.
   - an interrupt written just after the guest stopped may reach QEMU's stub while it is halted.
     The stop that gdb reports is the one returned; any effect on a later resume is observed by
     the live proof, not assumed.
   - a guest left running while its session is detached by the control plane: the existing
     force-crash and reboot detach does not reap engines today, and this design does not change
     that.
4. Covered elsewhere
   - interrupt reply stop: #3230 (merged). Probe pause: #3245 / ADR-0798 (merged).
   - cross-replica session ownership: existing lifecycle architecture (excluded).
   - ppc64le and remote-libvirt qualification: their own owners (#2818, #3113); x86 first.

No trust boundary is added or widened. Authorization, project gating, the audit trail and the
redaction of stop records are unchanged, so no threat model is required.

## Global Constraints

Python 3.14; existing dependencies only; preserve the x86_64 and ppc64le targets. No migration,
schema, lifecycle protocol, ADR index or persisted-data change. The implementing PR moves ADR-0783
to Accepted, per the ADR README's ratification rule.

## Validation

Engine tests use the existing fake MI controller
(`tests/providers/local_libvirt/test_debug_gdbmi.py`):

- `continue_background`:
  - `^running` only → `None` and the state is `running`.
  - A reply carrying `*stopped` → that stop, and the state is `halted`.
  - In `running` → no write.
- State on failure paths:
  - A resume write timeout leaves `running`.
  - A stalled interrupt-back (`transport_stall`) leaves `running`, and the next `interrupt`
    writes `-exec-interrupt`.
  - A resume `^error` returns to `halted`.
  - All of this holds for `continue_` and for each advance verb.
- `interrupt`:
  - The reply or the poll carries `signal-received` with `SIGINT` → `interrupt`; a
    `signal-received` with `SIGTRAP` → `stop_event`.
  - The reply carries `breakpoint-hit` → `stop_event`.
  - No stop → `transport_stall`, and the state stays `running`.
  - `attach_halted` → `attach`, and `halted` → `already_halted`. Both send zero writes and
    make no poll, which the injected sleep and controller reads show.
- `continue_` in `running`: no `-exec-continue` is written, the stop is reported, and a timeout
  interrupts back.

Runtime and tool tests (`tests/mcp/debug/test_debug_ops.py`):

- The guard refuses every op except continue and interrupt with `inferior_running` and its next
  actions, sending zero controller writes. The test is parametrized over the registered op
  factories and includes `list_modules` and `load_module_symbols`. Controlled fault: remove the
  guard and observe red.
- Covered for the tools: the `wait` schema field and the ignored `timeout_sec`, the `running`
  envelope, the interrupt `halted_by`/`reason` data and its stall next actions, and the
  continue audit row's `wait`.
- A real cancellation test: cancel a request whose engine call blocks on an event; assert
  `lock.locked()` stays `True` until the thread finishes, and a second op starts only after it.
  Controlled fault: a plain `shield` without the await loop must turn it red.
- An op queued behind a reap sees the session as not live and does not attach.

Session-end tests (under `tests/mcp/debug/`):

- A running attachment gets `-exec-interrupt` before `exit()`, off the event loop.
- A stalled halt adds `guest_execution="unknown"`.
- A halted attachment, a never-attached session and a drgn-live session get no interrupt and an
  unchanged reply (no `guest_execution` key).

The fault-inject engine's new answers are covered by its existing debug tests.

Live cell unit tests (`tests/integration/live_stack/test_debug_cells.py`) run the rewritten
interrupt body against a scripted session, including both breakpoint-arm paths and its failure
messages.

The rewritten `debug.interrupt` cell:

1. Start the session. `debug.continue(wait=false)` answers `running`, and the guest answers SSH,
   so it runs between calls.
2. Interrupt → `halted_by="interrupt"` with `signal-received`. The guest stops answering, and
   `rip` is a kernel address.
3. Breakpoint arm: set the `vfs_read` breakpoint and `debug.continue(wait=false)`.
   - If that reply is already `stopped` with `breakpoint-hit` (an ambient read), record that path.
   - Otherwise write the SSH banner, then `debug.continue(wait=true)` must report `breakpoint-hit`
     without resuming again.
   - Either path is recorded, and neither is retried to get the other.
4. Clear the breakpoint, then `debug.continue(wait=false)` and check the guest answers.
5. End the session while the guest runs: the reply is `detached` with no `guest_execution` key,
   and the guest answers again.

Add one run-control line to the served toolset guide `docs/guide/toolsets/debug.md` and its
resource copy `src/kdive/mcp/resources/_content/toolsets-debug.md`: `debug.continue` with
`wait=false` leaves the guest running, other tools refuse with `inferior_running` until
`debug.interrupt` or `debug.continue`. Regenerate `docs/guide/reference/debug.md` and
`src/kdive/cli/commands/_generated_verbs.py` with their existing recipes.
Gates: `just lint`, `just type`, focused tests, then the managed pre-push `just ci`.

Live proof needs separate operator admission:

- Target: x86_64 local-libvirt, default configuration, on a disposable Fedora 44 x86_64 KVM lab
  guest with the `longterm` fixture already built.
- Deploy the candidate head through `examples/local-libvirt/demo-up.sh`, with an on-disk server
  `TMPDIR` (#3229).
- Run the functional cells of `debug.interrupt`, `debug.continue`, `debug.advance`,
  `debug.end_session` and gdbstub `debug.start_session` from
  `tests/integration/test_debug_tool_cells_live.py`.
- Clean up with `demo-down.sh --wipe --yes`, then run the runbook's leftover-domain and staging
  checks.
- Estimated at 45–60 minutes with the fixture present. A fixture build adds its own cost and
  needs its own approval.

Offline green is not that proof.
