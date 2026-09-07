# 0628 — An expected crash admits a reachable gdbstub

## Status

Accepted (2026-09-07)

## Context

ADR-0064 §2 recorded that `debug.start_session` rejects a Run whose succeeded boot step carries
`boot_outcome = "expected_crash_observed"`, "because that Run is not a live-debuggable guest".
ADR-0233 narrowed that assertion for the *undeclared* crash path: a boot that panics without a
declared expectation records `crashed_halted_live` when the provisioned stub answers, and
`_attach_preconditions` admits gdbstub against it. ADR-0233's "Considered & rejected" list then
declined to extend the same admission to the declared path — "Reverse the declared expected-crash
(A/B) flow too. Rejected: `expected_crash_observed` is a deliberate ADR-0064 workflow that keeps
the System reusable and routes to post-mortem evidence; an operator running A/B kernel tests wants
that, not a live session."

That rejection bundled two separable changes. Reversing the *routing* — the recorded
`boot_outcome`, the reusable System, the post-mortem evidence path — is not the same as admitting
a *transport*, and only the second is needed to make a provisioned stub usable. Bundling them
turned "an A/B operator wants post-mortem routing" into "an A/B operator may not attach", a
stronger claim that was never established.

The cost is concrete. A System provisioned with `debug.gdbstub: true` and
`debug.preserve_on_crash: true` cannot be live-debugged if its Run declared an
`expected_boot_failure`, even with the guest halted and the stub answering, while the same panic
without the declaration takes the `crashed_halted_live` branch and is admitted. Declaring the
expectation — the thing that makes a reproducer run legible — is what costs the caller the
capability they provisioned for. `runs.get` compounds it by reporting
`inert_capture: ["gdbstub", …]` with no reachability probe at all, and `debug.start_session`
refuses with `CONSOLE_CRASH_GUIDANCE`, a detail written entirely about kexec and vmcore capture.
Neither is true of gdbstub: attaching to a preserved, vCPU-stopped guest needs no capture kernel
and produces no vmcore.

Two call sites reach `record_expected_crash`. The readiness-failure branch of
`jobs/handlers/runs/boot.py` reaches it when `booter.boot` raised `READINESS_FAILURE` — the guest
never reached `kdive-ready`. `evaluate_expected_failure_after_ready` (ADR-0383) reaches it from
the *ready* path, when the guest reached the marker and the console showed the declared signature
afterwards. That distinction turns out to decide where a probe is admissible.

## Decision

Narrow ADR-0233's rejection rather than superseding ADR-0233. Extend ADR-0233's admission to the
declared expected-crash path, for the gdbstub transport only, gated on the same evidence
ADR-0233 already requires.

1. **Probe, do not assume — from the readiness-failure call site only.** `record_expected_crash`
   runs the same `gdbstub_reachable` probe `record_crash_halted_live` uses, and only when
   `boot.py`'s `READINESS_FAILURE` branch supplies it a `Connector`.
   `evaluate_expected_failure_after_ready` supplies none and never probes: its guest reached
   `kdive-ready` by construction, so it may still be executing, and an RSP connect stops a live
   vCPU. When the stub answers, `gdbstub` moves from `inert_capture` to `available_capture`;
   otherwise both lists are unchanged.
2. **Keep ADR-0233's crash signal as the second gate.** Even on the readiness-failure path the
   probe runs only when the captured console matches the generic kernel-panic signature, exactly
   as `record_crash_halted_live` requires. ADR-0233 decision 3 established why: a readiness
   timeout can be a slow-but-healthy boot rather than a crash, and a declared expectation is a
   caller-supplied literal pattern, not a panic signature.
3. **Admit on recorded evidence.** `_attach_preconditions` admits `expected_crash_observed` for
   the gdbstub transport when the boot step's `available_capture` lists `gdbstub`, and refuses
   every other combination. `open_transport` still re-probes authoritatively at attach.
4. **Keep the two `runs.get` surfaces agreeing.** `_succeeded_next_step` names
   `debug.start_session` when the same `available_capture` lists `gdbstub`, so the next-action an
   agent follows cannot contradict the capture set in the same envelope.
5. **Give the gdbstub refusal its own words.** A gdbstub attach refused on this outcome carries a
   detail about the unreachable stub. `CONSOLE_CRASH_GUIDANCE` stays on the vmcore surfaces and
   on the non-gdbstub refusal, which it describes correctly.

What ADR-0233's rejection protected is untouched. `boot_outcome` stays `expected_crash_observed`,
`expectation_matched` stays true, the System stays `READY` and reusable for the next A/B Run, the
console artifact stays the evidence of record, `console` stays in `available_capture`, and
`postmortem.crash` stays among the next actions. An A/B operator who wants post-mortem routing
still gets exactly that; the change adds a path that was being withheld, and removes none.

`boot_outcome` is a schemaless value in `run_steps.result`, so there is no migration and no
state-machine change.

## Consequences

- A reproducer Run on a `gdbstub`-provisioned System whose boot failed readiness with a panicking
  console can start a live gdbstub session against its halted guest, and `runs.get` reports
  `gdbstub` as inert only after a probe says so, so the field stops asserting something it never
  checked.
- Boot slows by one bounded RSP probe, only on the readiness-failure path, and only when the
  console shows a generic panic and `gdbstub` is provisioned. Every other expected-crash boot
  keeps today's behavior exactly and runs no probe.
- A Run downgraded to `expected_crash_observed` after reaching readiness (ADR-0383) is never
  probed and so is never admitted, even if its guest did halt with a live stub. That is a
  deliberate false negative: the alternative risks stopping a running guest's vCPU, and the
  operator still has the console artifact and the post-mortem route. Lifting it needs a halt
  signal the console alone does not carry.
- `host_dump` keeps its unprobed place in `inert_capture` on this path. Only `gdbstub` is in
  scope here, so the same "asserted without checking" criticism still applies to that entry and
  is left for separate work.
- Boot steps recorded before this change carry `available_capture: ["console"]`, so a gdbstub
  attach against them is refused with the new detail rather than admitted — correct, because no
  probe ever ran for them.
- ADR-0064's A/B workflow is unchanged except for the sentence ADR-0233 had already narrowed.
  ADR-0233 remains Accepted and governs the undeclared path unchanged; only the one rejected
  alternative named above stops holding.
- The refusal still lands after the crash rather than at `runs.create`, which is out of scope
  here and likely infeasible: the stub's reachability is not knowable until the guest has booted
  and halted.

## Considered & rejected

- **Supersede ADR-0233 outright.** **judgment:** its five decisions all still hold and are the
  ground this one stands on, not the ground it replaces. Retiring the record to overturn one
  bullet in its rejected list would leave the undeclared path with no live authority.
- **Probe from both call sites, gated on the panic signature alone.** **verified:** ADR-0383's
  motivating console is a readiness marker followed by a panic, and the in-tree fixture
  `_MARKER_THEN_UBSAN_PANIC` (`tests/jobs/handlers/test_runs_boot.py:898`) is exactly that shape:
  `kdive-ready` then `Kernel panic - not syncing`. `generic_panic_matches` passes on it, so the
  signature does not establish a halted vCPU on the post-ready path, and ADR-0233's "Treat the
  RSP probe as the crash signal" rejection records that an RSP connect stops one that is running.
  Confining the probe to the readiness-failure call site is also the smaller change.
- **Gate the post-ready probe on `preserve_on_crash` instead.** **verified:** ADR-0233 decision 3
  records that the gate is deliberately "not `preserve_on_crash`", because live-gdb is meant to
  rescue a kdump- or host_dump-primary System too. Re-introducing it as a gate on one path only
  would make the flag mean two different things.
- **Reverse the routing too — record `crashed_halted_live` for a declared crash.** **verified:**
  ADR-0064 §2 binds `expected_crash_observed` to the reproduction verdict and to keeping the
  System reusable for the A/B pair, and `src/kdive/mcp/tools/lifecycle/runs/common.py` reads the
  outcome in both `_succeeded_next_step` and `_capture_data`. It would break the reproduction
  verdict to buy a transport that admitting the transport already buys.
- **Admit on the outcome alone, letting `open_transport` be the only probe.** **judgment:** it
  would report `inert_capture: ["gdbstub"]` while `debug.start_session` admitted the same stub —
  two surfaces disagreeing about one fact.
- **Give the gdbstub refusal a new `reason` value as well as a new detail.** **judgment:**
  `expected_crash_not_live_debuggable` stays accurate for a stub that did not answer, and the
  detail already carries the distinction.
- **Admit `drgn-live` too.** **verified:** ADR-0233's "Admit `drgn-live` to a halted crash"
  rejection records that the transport reaches the guest over SSH (ADR-0218) and a halted guest
  has no running sshd. Unchanged here.
- **Do nothing.** **judgment:** the capability stays withheld from precisely the runs that
  provisioned for it, and `inert_capture` keeps asserting an unprobed claim.
