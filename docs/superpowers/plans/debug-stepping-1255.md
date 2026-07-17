# Plan: gdb stepping tools (#1255)

- Spec: [2026-07-17-debug-stepping-1255-design.md](../specs/2026-07-17-debug-stepping-1255-design.md)
- ADR: [ADR-0379](../../adr/0379-gdb-source-and-instruction-stepping.md)
- Branch: `feat/debug-stepping-1255` — base `main`
- Guardrails: `just lint`, `just type` (whole-tree), `just test`; full gate `just ci`
  (lint + type + lint-shell + lint-workflows + check-mermaid + test). Single test:
  `uv run python -m pytest <path>::<name> -q`.

## Goal

Add four contributor-gated MCP tools — `debug.step`, `debug.next`, `debug.step_instruction`,
`debug.finish` — that resume a live gdb-MI `DebugSession` by one source line (into/over), one
machine instruction, or until the current frame returns, each returning the redacted
`GdbStopRecord`. No schema migration, no `DebugSessionState` change. Mirror the existing
`debug.continue`/`interrupt` vertical slice.

## Conventions (apply to every task)

- TDD: write the failing test first, watch it fail for the right reason, then implement.
- Wrapper docstring is the agent-facing contract; it must **not** cite ADRs
  (`tests/mcp/core/test_no_adr_leak.py`) and must name every field/constraint.
- Pick the most specific existing `ErrorCategory`; never invent strings.
- Every tool returns a `ToolResponse`; failures carry an `error_category`.
- Commit one logical change at a time, imperative subject ≤72 chars, with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## Task 1 — Engine methods, port protocol, verb-neutral timeout guard (TDD)

**Where it fits:** the transport layer that issues the MI verbs. `ExecutionControl.resume`
is already verb-generic, so each engine method is a one-line delegate.

**Files:**
- `src/kdive/providers/ports/debug.py` — add `step`, `next`, `step_instruction`, `finish` to
  the `GdbMiEngine` Protocol (mirror `continue_`'s signature + Raises docstring; all take
  `*, timeout_sec: float` and return `GdbStopRecord`).
- `src/kdive/providers/shared/debug_common/gdbmi/core/engine.py` — implement the four methods
  next to `continue_` (line ~309), each `return self._execution.resume(attachment, "<verb>",
  timeout_sec=timeout_sec)` with verbs `-exec-step`, `-exec-next`, `-exec-step-instruction`,
  `-exec-finish`.
- `src/kdive/providers/shared/debug_common/gdbmi/core/execution.py` — generalize the
  invalid-timeout guard in `resume` (line ~76-81): message → "gdb/MI resume timeout must be a
  finite non-negative number", code → `bad_resume_timeout`.

**Tests first** (`tests/providers/local_libvirt/test_debug_gdbmi.py`, mirror the continue set):
- For each verb: a `test_<verb>_returns_stop_on_breakpoint_hit`-style test scripting the fake
  controller keyed on the exact `-exec-*` string, asserting the redacted stop is returned.
- `test_finish_raises_on_outermost_frame_error` — fake returns `^error` for `-exec-finish`;
  assert `CategorizedError` / `DEBUG_ATTACH_FAILURE` is raised **without** polling the full
  wait (no `*stopped` scripted).
- `test_step_interrupts_on_timeout` — no stop scripted; assert interrupt-back +
  `timed_out=True` (mirror `test_continue_interrupts_on_timeout`).
- Migrate the two `bad_continue_timeout` assertions (lines ~652, ~880) to `bad_resume_timeout`
  and the new message.

**Acceptance:** `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -q`
green; each method issues its exact verb; timeout guard code is `bad_resume_timeout`.

## Task 2 — MCP tool wrappers + op closures + audit (TDD)

**Where it fits:** the agent-facing tools over the Task 1 engine methods.

**Files:**
- `src/kdive/mcp/tools/debug/operations/execution.py` — add `_register_debug_step`,
  `_register_debug_next`, `_register_debug_step_instruction`, `_register_debug_finish` (call
  them from `register`, lines 26-27) plus their `_EngineOp` closures (mirror `_continue_op`,
  calling `engine.step/next/step_instruction/finish`). Each returns
  `ToolResponse.success(session_id, "stopped", suggested_next_actions=[...],
  data=_stop_data(stop.reason, stop.timed_out))`. Suggested next actions steer toward
  `debug.read_registers`, `debug.backtrace`, and stepping again.
  - Wrapper docstrings: `session_id` + `timeout_sec` Fields as on `debug.continue`; one-line
    docstring naming into/over/instruction/return and (for step/next) telling the agent to use
    `debug.step_instruction` where the PC has no line info. End with "Requires contributor."
    No ADR references.
- `src/kdive/mcp/tools/debug/operations/runtime.py` — add `"debug.step"`, `"debug.next"`,
  `"debug.step_instruction"`, `"debug.finish"` to `_AUDITED_OPS` (line ~76).

**Tests first** (`tests/mcp/debug/test_debug_ops.py`):
- Add the four tools to the op dispatch map (lines ~281-282) and assert each returns `stopped`
  with the right `data` (`reason`/`timed_out`) and non-empty `suggested_next_actions`, mirroring
  `test_continue_returns_stopped`. Add a case asserting a `finish` `^error` surfaces as a failure
  `ToolResponse` with `DEBUG_ATTACH_FAILURE`.
- **Audit coverage:** add a per-op end-to-end audit-row test for each of the four ops (mirror
  `test_registered_set_breakpoint_handler_writes_audit_row`, line ~386) asserting exactly one
  `audit_log` row on success — this fails if the op is omitted from `_AUDITED_OPS`. Update the
  pinned expected set in `test_op_audit_descriptor_covers_only_mutating_and_sensitive_ops`
  (line ~347) to include the four names.

**Acceptance:** `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -q` green, including the
four audit-row tests and the updated pinned-set test.

## Task 3 — Enumeration registries (make the guards pass)

**Where it fits:** the cross-cutting registries every new tool must appear in.

**Files:**
- `src/kdive/mcp/exposure.py` — add the four names as `_CONTRIBUTOR` in the debug ACL block
  (after `debug.interrupt`, line ~127).
- `src/kdive/mcp/schema/tool_index.py` — add a keyword frozenset per tool (after
  `debug.interrupt`, line ~155): e.g. `step` → `{"step", "stepi"/"into", "line", "debug"}`,
  `next` → `{"next", "step", "over", "line", "debug"}`, `step_instruction` →
  `{"step", "instruction", "stepi", "asm", "debug"}`, `finish` →
  `{"finish", "return", "step", "debug"}`.
- `tests/mcp/core/test_tool_docs.py` — add the four names to the tool→test map (line ~72),
  each → `("tests/mcp/debug/test_debug_ops.py",)`.

**Acceptance:** `uv run python -m pytest tests/mcp/core/test_exposure.py tests/mcp/core/test_app.py tests/mcp/core/test_tool_docs.py -q` green (triaged==registered; every tool mapped to a test).

## Task 4 — Guide docs

**Where it fits:** served toolset/reference docs; the completeness guard requires the toolset
doc to name **exactly** the live tools in the namespace.

**Files:**
- `docs/guide/toolsets/debug.md` — under `## Run control` (lines 33-36, after
  `debug.interrupt`) add four bullets: `debug.step` — advance one source line into calls;
  `debug.next` — advance one source line over calls; `debug.step_instruction` — advance one
  machine instruction; `debug.finish` — run until the current frame returns.
- `docs/guide/reference/debug.md` — add matching reference entries alongside continue/interrupt.

**Acceptance:** `uv run python -m pytest tests/mcp/resources/test_toolset_doc_completeness.py -q`
green; `just check-mermaid` clean.

## Task 5 — Live smoke + live driver

**Where it fits:** the real-KVM proof (spec criterion #5); does not run in `just ci` (gated
`live_vm` marker) but is the functional-capability proof.

**Files:**
- `tests/mcp/debug/test_debug_gdbmi_live_smoke.py` — exercise all four verbs against real KVM
  in the promoted-ops smoke; assert each advances execution and returns a stop. Do **not**
  assert the outermost-frame refusal live (not reliably reachable without a frame-select op —
  see the spec Refused-verb contract); the no-hang mechanism is the fake unit test.
- `scripts/live-debug.py` — add a step exercise near the existing `debug.continue` call
  (line ~430).

**Acceptance:** the live smoke is collectable (`-m live_vm` selects it); run it on this KVM
host as the functional proof before shipping.

## Full-suite gate

After all tasks: `just ci` green. Then run the live proof (Task 5) on this host per the
"functional test drives capability" rule.

## Rollback

Pure additive feature: revert the branch. No migration to reverse, no state introduced. The
only edit to existing behavior is the `bad_continue_timeout` → `bad_resume_timeout` rename,
reverted with the branch.
