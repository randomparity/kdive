# Spec: Complete the gdb-MI debug plane with stepping (#1255)

- Issue: #1255 "Complete Debug Tool Features"
- ADR: [ADR-0379](../../adr/0379-gdb-source-and-instruction-stepping.md)
- Status: Design accepted

## Problem

The gdb-MI debug plane drives a live gdbstub `DebugSession` and exposes `debug.continue`
and `debug.interrupt`, but no stepping. After a breakpoint fires, an agent that wants to
walk the next few source lines has to set a breakpoint on the following line, `continue`,
then clear it — and stepping *over* a call needs a hand-computed temporary breakpoint on the
return address. The issue asks to complete the plane with `debug.step`, `debug.step_instruction`,
and `debug.finish` (this design adds `debug.next` — see ADR-0379).

## Requirement (restated)

Add interactive stepping to the live gdb-MI debug session so an agent can advance execution
by one source line (into or over calls), by one machine instruction, or run until the current
frame returns — without breakpoint churn — and observe the resulting stop.

## Tool surface

Four contributor-gated MCP tools, each mirroring `debug.continue`'s shape (a `session_id` and
an optional `timeout_sec`, returning the redacted `GdbStopRecord` as `{reason, timed_out}`):

| tool | MI verb | behavior |
|------|---------|----------|
| `debug.step` | `-exec-step` | advance one source line, stepping into called functions |
| `debug.next` | `-exec-next` | advance one source line, stepping over called functions |
| `debug.step_instruction` | `-exec-step-instruction` | advance one machine instruction |
| `debug.finish` | `-exec-finish` | resume until the current frame returns |

## Behavior contract

- **Precondition:** an existing `DebugSession` in `LIVE` state, resolved and role-gated by the
  shared `_live_session` path. Stepping does not change the session state.
- **Success:** returns `ToolResponse.success(session_id, "stopped", data={reason, timed_out})`
  with `suggested_next_actions` steering toward reading state and stepping again.
- **Timeout:** `timeout_sec=0.0` uses the provider interactive wait cap; a positive value bounds
  the wait. If the wait elapses (e.g. `finish` on a frame that does not return, or a step over a
  call that runs long or hits nothing), the engine interrupts back and returns `timed_out=True` —
  identical to `continue`.
- **Refused verb:** a verb gdb rejects synchronously — e.g. `finish` when the selected frame is
  the outermost (unwind-terminal) one — surfaces as `CategorizedError(DEBUG_ATTACH_FAILURE)`
  with the redacted gdb message, the same path every other MI-command error takes. No
  60-second hang, because `execute_mi_command` raises on the `^error` result before `resume`
  ever waits. This synchronous-refusal *mechanism* is proven deterministically by the
  fake-controller unit test (criterion #3); it is **not** asserted against real gdb, because
  `-exec-finish` acts on gdb's *selected* frame, which is always frame #0 (innermost) after a
  stop, and the plane exposes no frame-select op — so forcing the outermost-frame refusal live
  would depend on the smoke test landing at an unwind-terminal frame #0 (location- and
  unwind-quality-dependent), not a property this feature controls. `finish` at a normal
  breakpoint therefore runs to frame #0's caller and returns a regular stop; that functional
  behavior is what the live smoke proves.
- **Missing line info (`step`/`next`):** these verbs need line-number information for the
  current PC. Where a live kernel has none (a stripped region, or a module whose symbols were
  not loaded via `debug.load_module_symbols`), gdb does not error — `-exec-step`/`-exec-next`
  single-step until control reaches a line with info, which over such code can run out the
  bounded wait and return `timed_out=True` at an unrelated frame. This is the same
  timeout+interrupt path as a long-running `continue`; an agent that wants deterministic
  instruction-granular progress in a no-line-info region uses `debug.step_instruction`. The
  `step`/`next` wrapper docstrings state this so the agent-facing schema is the contract.
- **Invalid timeout:** a negative or non-finite `timeout_sec` raises
  `CONFIGURATION_ERROR`. The shared `ExecutionControl.resume` guard is generalized to name the
  resume family, not `continue` specifically (message "gdb/MI resume timeout must be a finite
  non-negative number", code `bad_resume_timeout`), so the error names the operation the agent
  actually called — the two existing `continue` guard tests move to the new code.
- **Redaction / audit / transcript:** unchanged — the stop record is redacted, the four ops are
  audited alongside `continue`/`interrupt`, and each MI command is appended to the session
  transcript by the existing machinery.

## Success criteria (falsifiable)

1. `debug.step`/`next`/`step_instruction`/`finish` are registered, contributor-gated, and
   discoverable (exposure ACL, tool-index keywords, tool-doc map — the existing enumeration
   guards pass).
2. Each engine method issues its exact MI verb through `ExecutionControl.resume` and returns the
   redacted stop — asserted by a unit test that scripts the fake controller keyed on the verb
   string (mirroring `test_continue_returns_stop_on_breakpoint_hit`).
3. A `finish` whose MI result is `^error` raises `DEBUG_ATTACH_FAILURE` without waiting out the
   timeout — asserted by a unit test.
4. A timed-out step interrupts back and returns `timed_out=True` — asserted by a unit test
   (mirroring `test_continue_interrupts_on_timeout`).
5. The live smoke test exercises all four new verbs against real KVM (added to the promoted-ops
   smoke), asserting each advances execution and returns a stop — proving the resume path works
   on real gdb, not only the fake. It does **not** assert the outermost-frame refusal (see the
   Refused-verb contract: not reliably reachable without a frame-select op); the no-hang
   *mechanism* is criterion #3's deterministic fake test. `scripts/live-debug.py` demonstrates a
   step.
6. A negative/non-finite `timeout_sec` on any of the four tools raises `CONFIGURATION_ERROR`
   with code `bad_resume_timeout` (verb-neutral) — asserted by the migrated guard tests.
7. Each of the four ops is audited: it writes exactly one `audit_log` row on success (asserted
   by a per-op end-to-end test mirroring `test_registered_set_breakpoint_handler_writes_audit_row`),
   and the pinned `_AUDITED_OPS` drift test
   (`test_op_audit_descriptor_covers_only_mutating_and_sensitive_ops`) is updated to include the
   four names. A registered op omitted from `_AUDITED_OPS` fails its own audit-row test.
8. `just ci` is green.

## Out of scope

- `-exec-next-instruction` (step over one instruction) and a step-count/repeat parameter — see
  ADR-0379 rejected alternatives.
- A frame-select op (`-stack-select-frame`). `finish` acts on the innermost frame #0 after a
  stop; selecting an outer frame to finish is a separate capability #1255 does not ask for.
- A broad "every mutating `debug.*` tool ∈ `_AUDITED_OPS`" structural guard: the invariant is
  false, because `debug.start_session`/`end_session` are mutating yet deliberately audited via
  their attach/detach rows, not `_AUDITED_OPS`. The per-op audit-row tests (criterion #7) are
  the falsifiable backstop instead.
- Any `DebugSessionState` change, schema migration, or new provider port beyond the four
  methods on the existing `GdbMiEngine`.

## Touch points (from the plane map)

- Engine: `providers/shared/debug_common/gdbmi/core/engine.py` (4 methods) +
  `providers/ports/debug.py` `GdbMiEngine` protocol (4 declarations); generalize the
  invalid-timeout guard in `providers/shared/debug_common/gdbmi/core/execution.py`
  `resume` to `bad_resume_timeout` and update the two guard tests in
  `tests/providers/local_libvirt/test_debug_gdbmi.py`.
- Tool: `mcp/tools/debug/operations/execution.py` (4 registrations + op closures).
- Registries: `mcp/tools/debug/operations/runtime.py` `_AUDITED_OPS`; `mcp/exposure.py` ACL;
  `mcp/schema/tool_index.py` keywords; `tests/mcp/core/test_tool_docs.py` tool→test map.
- Tests: `tests/mcp/debug/test_debug_ops.py` (tool dispatch; per-op audit-row tests mirroring
  `test_registered_set_breakpoint_handler_writes_audit_row`; update the pinned expected set in
  `test_op_audit_descriptor_covers_only_mutating_and_sensitive_ops`),
  `tests/providers/local_libvirt/test_debug_gdbmi.py` (engine/verb),
  `tests/mcp/debug/test_debug_gdbmi_live_smoke.py` (live), `scripts/live-debug.py`.
- Docs: `docs/guide/reference/debug.md`, `docs/guide/toolsets/debug.md`.
