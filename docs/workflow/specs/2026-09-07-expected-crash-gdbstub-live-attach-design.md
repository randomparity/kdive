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

Four changes, per ADR-0628, plus the agent-facing text that discloses them.

1. `record_expected_crash` takes an optional `Connector` and runs the `gdbstub_reachable` probe
   already used by `record_crash_halted_live`, gated on that connector being supplied, on
   `gdbstub` being provisioned, and on the captured console matching `generic_panic_matches`. On
   a reachable stub, `gdbstub` moves from `inert_capture` into `available_capture`; `console`
   stays in `available_capture` either way. Only `boot.py`'s `READINESS_FAILURE` branch supplies
   the connector — it already holds one. `evaluate_expected_failure_after_ready` supplies none
   and is otherwise untouched: its guest reached `kdive-ready`, so it may still be executing and
   an RSP connect would stop a live vCPU (ADR-0628 decision 1, ADR-0233 decision 3).
2. `_attach_preconditions` admits `expected_crash_observed` for the `gdbstub` transport when the
   succeeded boot step's `available_capture` lists `gdbstub`, and refuses otherwise.
3. The gdbstub refusal gets its own module-level detail constant; `CONSOLE_CRASH_GUIDANCE` stays
   on the non-gdbstub refusal and on the vmcore surfaces.
4. `_succeeded_next_step` (`src/kdive/mcp/tools/lifecycle/runs/common.py`) names
   `debug.start_session` when that same `available_capture` lists `gdbstub`. Without this the one
   `runs.get` envelope would report an attachable stub while steering the agent to
   `vmcore.fetch`, which that function's own docstring says always rejects on this outcome.

Also in scope: the `debug.start_session` wrapper docstring, which is the agent-facing contract
(AGENTS.md).

Out of scope, per the frozen charter: any change to the recorded `boot_outcome`; reversing
routing for non-gdbstub transports; an on-panic action knob; raising the refusal at
`runs.create`; and #802's `inert_capture_reason` disclosure work. Also deliberately excluded:
probing `host_dump`, and the pre-existing emission of `inert_capture_reason` beside an empty
`inert_capture` — both are reachable at HEAD without this change, and the second is pinned by
existing tests. No deferrals carried in; two follow-up candidates carried out.

## Success

1. A boot recording `expected_crash_observed` from the readiness-failure path, on a
   gdbstub-provisioned System whose console panics and whose stub answers, reports
   `available_capture` containing `gdbstub` and `inert_capture` without it.
2. The same boot with an unreachable stub reports today's lists and probes exactly once. With an
   unprovisioned stub, or a console matching the declared expectation without a generic panic, it
   reports today's lists and never probes.
3. A boot downgraded by `evaluate_expected_failure_after_ready` never probes and reports today's
   lists, whatever its console shows.
4. `debug.start_session(run, "gdbstub")` on outcome 1 falls through to the existing
   System-ready/occupied checks and attaches.
5. `debug.start_session(run, "gdbstub")` on outcomes 2 and 3 is refused with the new gdbstub
   detail.
6. `debug.start_session(run, "drgn-live")` on any `expected_crash_observed` run is refused with
   `CONSOLE_CRASH_GUIDANCE`, unchanged.
7. `runs.get` on outcome 1 names `debug.start_session` in `suggested_next_actions`; on outcomes 2
   and 3 it names today's `["postmortem.crash", "vmcore.fetch"]` unchanged.
8. The `debug.start_session` wrapper docstring states the admission and its precondition.

## Validation

The plan's per-task Verification inventories carry the concrete tests, red observations, and
green commands. Mapped to the success criteria above:

- Successes 1, 2, and 3 — focused-test, `tests/jobs/handlers/test_runs_boot.py` (plan Task 1):
  the capture pair across every gate combination, and a connector fake proving no probe runs on
  the unprobed ones.
- Successes 4, 5, and 6 — focused-test, `tests/mcp/debug/test_debug_tools.py` (plan Task 2).
- Success 7 — focused-test, `tests/mcp/lifecycle/test_runs_tools.py` (plan Task 3).
- Success 8 — task-test-not-applicable. The changed surface is prose in the `@app.tool`
  docstring FastMCP serializes; the only executable observation would search for or snapshot
  that wording, which the plan contract forbids. Reviewed against AGENTS.md instead.
- Whole tree — `just lint`, `just type`, `just adr-status-check`, `just test`, `just ci`.
