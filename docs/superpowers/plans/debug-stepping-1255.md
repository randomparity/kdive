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
- Commit with an imperative subject ≤72 chars and the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## Commit boundaries (every committed state must pass `just ci` — bisectability)

The task numbering is an implementation order, **not** a commit-per-task mapping. Two
whole-registry equality guards force some tasks to land together:

- `test_app.py` asserts the exposure classification set **equals** the live tool registry.
- `test_toolset_doc_completeness.py` requires the toolset doc to name **exactly** the live
  tools in the namespace.

So a commit that registers the four tools without also updating the ACL / tool-index / tool-doc
map / toolset doc leaves `just ci` **red**. Commit in these atomic units:

- **Commit A = Task 1** — engine methods + port + timeout-guard rename + their unit tests. No
  tools are registered yet, so the registry guards are unaffected; the new methods are covered
  by Task 1's unit tests. Green on its own.
- **Commit B = Tasks 2 + 3 + 4 together** — tool registration, `_AUDITED_OPS`, exposure ACL,
  tool-index keywords, `test_tool_docs` map, audit tests, **and** the guide docs, all in one
  commit. This is the smallest green state. Per-task acceptance commands below are progress
  checks, not commit boundaries.
- **Commit C = Task 5** — live smoke + `scripts/live-debug.py` (test/script only, `live_vm`-gated,
  so `just ci` stays green).

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
- `test_step_raises_on_missing_function_bounds` — fake returns `^error`
  ("Cannot find bounds of current function") for `-exec-step`; assert `DEBUG_ATTACH_FAILURE`
  (pins symbol-poor sub-case (b), mirroring the finish `^error` test).
- Migrate the three `bad_continue_timeout` sites to the new code/message (verified the only
  references, so Commit A is self-contained): `test_debug_gdbmi.py:652` (code assert),
  `:656` (message assert), `:880` (code assert).

**Acceptance:** `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -q`
green; each method issues its exact verb; timeout guard code is `bad_resume_timeout`.

## Task 2 — MCP tool wrappers + op closures + audit (TDD)

**Where it fits:** the agent-facing tools over the Task 1 engine methods.

**Files:**
- `src/kdive/mcp/tools/debug/operations/execution.py` — add `_register_debug_step`,
  `_register_debug_next`, `_register_debug_step_instruction`, `_register_debug_finish` (call
  them from `register`, lines 26-27) plus their `_EngineOp` closures (mirror `_continue_op`,
  calling `engine.step/next/step_instruction/finish`). Each wrapper is decorated
  `@app.tool(name="debug.<op>", annotations=_docmeta.mutating(), meta=_gdbmi_maturity())`
  exactly as `debug.continue` — the `mutating` classification and maturity are load-bearing for
  the flat-param and audit-set guards, so they must be explicit, not assumed. Each returns
  `ToolResponse.success(session_id, "stopped", suggested_next_actions=[...],
  data=_stop_data(stop.reason, stop.timed_out))`. Suggested next actions steer toward
  `debug.read_registers`, `debug.backtrace`, and stepping again.
  - Wrapper docstrings: `session_id` + `timeout_sec` Fields as on `debug.continue`; one-line
    docstring naming into/over/instruction/return. For `step`/`next`, state that a symbol-poor
    region may return either `timed_out=True` or a `DEBUG_ATTACH_FAILURE`
    ("Cannot find bounds of current function") and that `debug.step_instruction` is the fallback
    for both. End with "Requires contributor." No ADR references.
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

**Context constraint:** the promoted-ops smoke (`test_debug_gdbmi_live_smoke.py:77`) boots an
**early-panicking** kernel and halts in the `panic()` (noreturn) path — it never continues to a
returnable frame. So `finish`/`step`/`next` must **not** be exercised there: `finish` on a
noreturn frame runs out the 60s wait cap and returns `timed_out=True` (a 60s hang + a false
"returns a stop" assertion), and source-line stepping through panic code is unrepresentative.
Only `step_instruction` (one machine instruction, terminates immediately from any halt) is safe
in the panic context. Split the proof accordingly:

**Files:**
- `tests/mcp/debug/test_debug_gdbmi_live_smoke.py` — in the existing panic-halted smoke, assert
  **only** `debug.step_instruction`: it advances one machine instruction and returns a stop
  (representative of a halt-anywhere step). Do not add `finish`/`step`/`next` here. Assert the
  step **advanced the PC** — the smoke already disassembles the halt point, so compare the
  post-step instruction pointer against it — rather than merely "returned a stop", so a step
  stuck on a `hlt`-with-interrupts-disabled PC fails visibly. (`step_instruction` terminates
  promptly from a `cpu_relax`-style spin; it is not guaranteed instantaneous from every halt.)
- `scripts/live-debug.py` — add a stepping exercise that reaches a **resumable, returnable**
  frame the way `_stopped` already does (`set_breakpoint(<sym>)` + `debug.continue`,
  lines ~418-430) on a normally-booted kernel, then drives `debug.step`, `debug.next`,
  `debug.step_instruction`, and `debug.finish`. The breakpoint symbol must be a **promptly-
  returning, same-stack** hot function — **not** `schedule`/`DEFAULT_BREAK_SYMBOL` (a
  context-switch function: `finish` does not return on the same stack — it yields the CPU and
  only "returns" when this task is rescheduled, which can exceed the wait cap and time out; and
  single-stepping across `context_switch`/`__switch_to` confuses gdb). Pass an explicit
  `--symbol` (do not reuse the scheduler default): pick a leaf-ish syscall/VFS helper that
  returns to its caller on the same stack within the wait cap (e.g. `ksys_read`/`vfs_read`),
  **verified present and hit on the booted kernel** on this host. State expected outcomes so the
  run is falsifiable and non-hanging: `step`/`next`/`step_instruction` return a stop at an
  advanced PC; `finish` returns a clean stop at the breakpoint frame's caller and — critically —
  **`timed_out=False`**, so a scheduler-style non-return fails the gate loudly instead of passing
  as "a stop". This is the full four-verb functional proof run on this KVM host.
- Do **not** assert the outermost-frame refusal live (not reliably reachable without a
  frame-select op — see the spec Refused-verb contract); the no-hang mechanism is the fake unit
  test (Task 1).

**Acceptance:** the smoke is collectable (`-m live_vm`) and its `step_instruction` assertion
passes on this host; the `scripts/live-debug.py` stepping exercise walks all four verbs to a
clean, non-hanging finish on this KVM host. This is a **hard pre-merge gate** (see below), not a
best-effort — the tools ship maturity `implemented`, which asserts a live-proven capability.

## Full-suite gate

After all tasks: `just ci` green. **Hard pre-merge gate:** the four tools ship maturity
`implemented`, which under ADR-0175 asserts a live-proven capability — but the live proof is
`live_vm`-gated and does **not** run in `just ci`. So a green `just ci` alone is **not**
sufficient to merge. Before merge, run the Task 5 live proof on this KVM host (the smoke's
`step_instruction` assertion + the `scripts/live-debug.py` four-verb stepping exercise walking to
a clean finish) and confirm it passes. Do not merge on green CI until that proof passes.

## Rollback

Pure additive feature: revert the branch. No migration to reverse, no state introduced. The
only edit to existing behavior is the `bad_continue_timeout` → `bad_resume_timeout` rename,
reverted with the branch.
