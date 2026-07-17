# ADR-0379: gdb source and instruction stepping over the gdbstub (#1255)

- Status: Accepted
- Date: 2026-07-17

## Context

The gdb-MI debug tier (ADR-0034, extended by ADR-0248 symbol resolution, ADR-0275
backtrace/frame, ADR-0276 disassemble, ADR-0277 watchpoints, ADR-0278 module symbols)
drives a live gdbstub `DebugSession` through a persistent `gdb --interpreter=mi3` engine.
ADR-0034 scoped the interactive execution surface to `continue` and `interrupt`. There is
no way to advance execution by a single source line or machine instruction, or to run until
the current frame returns.

When the kernel stops at a breakpoint, an agent that wants to walk the next few lines has to
simulate stepping: set a breakpoint on the following line, `continue`, then clear it — the
set/clear breakpoint churn issue #1255 calls out. Stepping *over* a call is worse: it needs a
temporary breakpoint on the return address computed by hand.

gdb-MI already exposes the stepping verbs as part of the same resume family as
`-exec-continue`: `-exec-step` (one source line, into calls), `-exec-next` (one source line,
over calls), `-exec-step-instruction` (one machine instruction), and `-exec-finish` (run
until the selected frame returns). The engine's `ExecutionControl.resume(verb, timeout_sec)`
is already verb-generic — it was written for `-exec-continue` but takes any resume verb,
captures an early `*stopped` (ADR-0216), polls for the stop otherwise, and interrupts back on
timeout. `execute_mi_command` already raises `CategorizedError` (`DEBUG_ATTACH_FAILURE`) on a
`^error` result, so a verb that gdb refuses synchronously (e.g. `-exec-finish` in the
outermost frame) surfaces immediately rather than waiting out the timeout.

This is a completion of an existing plane, not a new one: the transport, session state, audit
path, and redaction are already in place.

## Decision

Add four resume-family ops to the **shared** `GdbMiEngine` (so both local-libvirt and
remote-libvirt gain them, matching ADR-0275's shared-engine precedent), each a one-line
delegate to the existing `ExecutionControl.resume`:

| engine method | MI verb | meaning |
|---------------|---------|---------|
| `step` | `-exec-step` | one source line, stepping into calls |
| `next` | `-exec-next` | one source line, stepping over calls |
| `step_instruction` | `-exec-step-instruction` | one machine instruction |
| `finish` | `-exec-finish` | run until the current frame returns |

Expose them as four contributor-gated MCP tools — `debug.step`, `debug.next`,
`debug.step_instruction`, `debug.finish` — mirroring `debug.continue`: a `session_id` plus a
`timeout_sec` (`0.0` uses the provider interactive wait cap), returning the redacted
`GdbStopRecord` as `{reason, timed_out}`. `debug.step` follows gdb's `step` (into); `debug.next`
is the step-over counterpart, added because the transport already supports the verb and it
directly removes the breakpoint churn #1255 cites for stepping over a call.

The `DebugSession` stays `LIVE` throughout, exactly as `continue`/`interrupt` do — stepping is
an operation *within* a live attachment, not a state transition. The four tools are audited
alongside `continue`/`interrupt` (`_AUDITED_OPS`), classified `_CONTRIBUTOR` in exposure, and
carry the shared `implemented` gdb-MI maturity.

## Consequences

- No schema migration and no `DebugSessionState` change — stepping reuses the `LIVE` state and
  the existing resume/wait/interrupt/redaction machinery.
- A timed-out `finish` (the frame never returns within the wait) interrupts back and returns
  `timed_out=True`, identical to `continue`. A synchronously-refused verb surfaces as
  `DEBUG_ATTACH_FAILURE` with the redacted gdb message, consistent with every other MI-command
  error, because `execute_mi_command` raises on the `^error` result before `resume` waits.
- `finish` acts on gdb's *selected* frame, which is frame #0 (innermost) after any stop — the
  plane exposes no frame-select op, so `finish` runs to the current function's caller. The
  outermost-frame refusal (`^error`) only arises when frame #0 is itself unwind-terminal, which
  on a live gdbstub kernel is location-dependent; the no-hang mechanism is therefore proven by a
  deterministic fake-controller unit test, not a live assertion that would hinge on unwind
  quality.
- The four ops are audited via `_AUDITED_OPS`, backed by a per-op end-to-end audit-row test so
  that omitting one from the set fails CI rather than shipping an unaudited live-execution op.
  A broad "mutating ⇒ audited" structural guard is not used: `start_session`/`end_session` are
  mutating but audited through their attach/detach rows, so that invariant does not hold.
- The four tool names must be added to every tool registry the guards enforce: `exposure.py`
  ACL, `tool_index.py` search keywords, and the `test_tool_docs.py` tool→test map. The wrapper
  docstrings state the into/over/instruction/return semantics so the agent-facing schema is the
  contract (per the wrapper-docstring rule).
- `debug.step`/`next`/`step_instruction` are source-and-instruction granular, so they need
  loaded debuginfo for `step`/`next` line boundaries — the same debuginfo the backtrace/frame
  ops (ADR-0275) already assume; `step_instruction` works without line tables. Where the current
  PC has no line information (a stripped kernel region or a module whose symbols were not loaded),
  gdb does not error: `-exec-step`/`-exec-next` single-step until control reaches a line with
  info, which over such code can run out the bounded wait and return `timed_out=True` at an
  unrelated frame — the same timeout+interrupt path as a long-running `continue`. The `step`/`next`
  wrapper docstrings tell the agent to use `step_instruction` for deterministic progress in a
  no-line-info region, so the degradation is a stated contract rather than a silent surprise.
- The reused `ExecutionControl.resume` invalid-timeout guard is generalized from the
  `continue`-specific message/code (`bad_continue_timeout`) to a verb-neutral one
  (`bad_resume_timeout`, "gdb/MI resume timeout ..."), so a bad `timeout_sec` on `debug.step`
  et al. names the resume family rather than misnaming the operation as `continue`. The two
  existing `continue` guard tests move to the new code; `continue`'s behavior is otherwise
  unchanged.

## Considered & rejected

- **Ship only the three tools the issue names, without `debug.next`.** Rejected: the transport
  is already verb-generic, so `next` costs one engine method and one tool wrapper, and stepping
  *over* a call is the exact case that otherwise needs the temporary-breakpoint churn #1255
  wants gone. Omitting it would leave the churn half-solved.
- **Make `debug.step` mean step-over (`-exec-next`).** Rejected: it diverges from gdb's `step`,
  which every agent and operator already reads as step-into; the surprise is not worth saving
  one tool.
- **A distinct `RUNNING`/`STEPPING` session state.** Rejected: `continue`/`interrupt` already
  operate within `LIVE` and gdb owns the run/stop status transiently in the `*stopped` record;
  a session-level state would duplicate it and add transitions with no reader.
- **Re-categorize `finish`-in-outermost-frame away from `DEBUG_ATTACH_FAILURE`.** Rejected: it
  is one of many gdb `^error` results and inventing a new category for it breaks the uniform
  MI-command error handling for no agent benefit.
- **Add `-exec-next-instruction` too.** Rejected: not requested, and `step_instruction` covers
  instruction-granular walking; a step-over-instruction verb has no demonstrated need (YAGNI).
- **A repeat/step-count parameter.** Rejected: an agent loops the tool if it wants N steps, and
  a count param would need its own bounding and timeout accounting for no demonstrated need.
