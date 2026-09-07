# Expected-crash gdbstub live attach — design

Issue: [#2303](https://github.com/randomparity/kdive/issues/2303).
Decision: [ADR-0628](../../adr/0628-expected-crash-admits-a-reachable-gdbstub.md).

## Problem

A Run that declares `expected_boot_failure` can never be live-debugged over gdbstub, even on a
System provisioned with `debug.gdbstub: true` and `debug.preserve_on_crash: true`, and even when
the stub answers on the halted guest. `record_expected_crash`
(`src/kdive/jobs/handlers/runs/boot_evidence.py`) sets `available_capture: ["console"]`
unconditionally and lists `gdbstub` in `inert_capture` whenever it is provisioned, with no
reachability probe; `_attach_preconditions`
(`src/kdive/mcp/tools/debug/sessions/lifecycle.py`) then refuses every transport on
`BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED` with `CONSOLE_CRASH_GUIDANCE`, a detail written entirely
about kexec and vmcore capture. The identical panic *without* the declaration takes the
`record_crash_halted_live` branch, is probed, and is admitted.

## Scope

Three changes, per ADR-0628, plus the agent-facing text that discloses them.

1. `record_expected_crash` runs the `gdbstub_reachable` probe already used by
   `record_crash_halted_live`, gated on `gdbstub` being provisioned **and** the captured console
   matching `generic_panic_matches`. On a reachable stub, `gdbstub` moves from `inert_capture`
   into `available_capture`; `console` stays in `available_capture` either way. This needs a
   `Connector` threaded to `record_expected_crash` and through
   `evaluate_expected_failure_after_ready`, from the `connector` already held by
   `_run_boot_and_capture_outcome` in `src/kdive/jobs/handlers/runs/boot.py`.
2. `_attach_preconditions` admits `expected_crash_observed` for the `gdbstub` transport when the
   succeeded boot step's `available_capture` lists `gdbstub`, and refuses otherwise.
3. The gdbstub refusal gets its own module-level detail constant; `CONSOLE_CRASH_GUIDANCE` stays
   on the non-gdbstub refusal and on the vmcore surfaces.

Also in scope: the `debug.start_session` wrapper docstring, which is the agent-facing contract
(AGENTS.md); and suppressing `inert_capture_reason` in
`src/kdive/mcp/tools/lifecycle/runs/common.py` when the probe empties `inert_capture`, so
`runs.get` cannot emit a kexec-worded reason for an empty list.

Out of scope, per the frozen charter: any change to the recorded `boot_outcome`; reversing
routing for non-gdbstub transports; an on-panic action knob; raising the refusal at
`runs.create`; and #802's `inert_capture_reason` disclosure work. No deferrals carried in.

## Success

1. A boot recording `expected_crash_observed` on a gdbstub-provisioned System whose console
   panics and whose stub answers reports `available_capture` containing `gdbstub` and
   `inert_capture` without it.
2. The same boot with an unreachable stub, an unprovisioned stub, or a console that matches the
   declared expectation but shows no generic panic reports today's lists unchanged and runs no
   probe in the last two cases.
3. `debug.start_session(run, "gdbstub")` on outcome 1 falls through to the existing
   System-ready/occupied checks and attaches.
4. `debug.start_session(run, "gdbstub")` on outcome 2 is refused with the new gdbstub detail.
5. `debug.start_session(run, "drgn-live")` on any `expected_crash_observed` run is refused with
   `CONSOLE_CRASH_GUIDANCE`, unchanged.
6. `runs.get` emits `inert_capture_reason` only alongside a non-empty `inert_capture`.
7. The `debug.start_session` wrapper docstring states the admission and its precondition.

## Validation

The plan's per-task Verification inventories carry the concrete tests, red observations, and
green commands. Mapped to the success criteria above:

- Successes 1 and 2 — focused-test, `tests/jobs/handlers/test_runs_boot.py` (plan Task 1): the
  capture pair across all four gate combinations, and a connector fake proving no probe runs on
  the two unprobed combinations.
- Successes 3, 4, and 5 — focused-test, `tests/mcp/debug/test_debug_tools.py` (plan Task 2).
- Success 6 — focused-test, `tests/mcp/lifecycle/test_runs_tools.py` (plan Task 3).
- Success 7 — task-test-not-applicable. The changed surface is prose in the `@app.tool`
  docstring FastMCP serializes; the only executable observation would search for or snapshot
  that wording, which the plan contract forbids. Reviewed against AGENTS.md instead.
- Whole tree — `just lint`, `just type`, `just adr-status-check`, `just test`, `just ci`.
