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

## Decision

Narrow ADR-0233's rejection rather than superseding ADR-0233. Extend ADR-0233's admission to the
declared expected-crash path, for the gdbstub transport only, gated on the same evidence
ADR-0233 already requires.

1. **Probe, do not assume.** `record_expected_crash` runs the same `gdbstub_reachable` probe
   `record_crash_halted_live` uses. When the stub answers, `gdbstub` moves from `inert_capture`
   to `available_capture`; otherwise both lists are unchanged.
2. **Keep ADR-0233's crash signal.** The probe runs only when the captured console also matches
   the generic kernel-panic signature, exactly as `record_crash_halted_live` requires. ADR-0233
   decision 3 established why: connecting an RSP client to a QEMU `-gdb` stub stops the vCPU, so
   the probe is not passive and must not run against a guest that may be healthy. A declared
   expectation is a caller-supplied literal pattern, not a panic signature, and the post-ready
   downgrade path (ADR-0383) can match it on a guest that is still running. The panic signature
   is what makes the probe safe on both paths.
3. **Admit on recorded evidence.** `_attach_preconditions` admits `expected_crash_observed` for
   the gdbstub transport when the boot step's `available_capture` lists `gdbstub`, and refuses
   every other combination. `open_transport` still re-probes authoritatively at attach.
4. **Give the gdbstub refusal its own words.** A gdbstub attach refused on this outcome carries a
   detail about the unreachable stub. `CONSOLE_CRASH_GUIDANCE` stays on the vmcore surfaces and
   on the non-gdbstub refusal, which it describes correctly.

What ADR-0233's rejection protected is untouched. `boot_outcome` stays `expected_crash_observed`,
`expectation_matched` stays true, the System stays `READY` and reusable for the next A/B Run, the
console artifact stays the evidence of record, and `console` stays in `available_capture`. An A/B
operator who wants post-mortem routing still gets exactly that; the change adds a path that was
being withheld, and removes none.

`boot_outcome` is a schemaless value in `run_steps.result`, so there is no migration and no
state-machine change.

## Consequences

- A reproducer Run on a `gdbstub`-provisioned System can start a live gdbstub session against its
  halted guest, and `runs.get` reports `gdbstub` as inert only after a probe says so, so the
  field stops asserting something it never checked.
- Boot slows by one bounded RSP probe on the expected-crash path, and only when the console shows
  a generic panic and `gdbstub` is provisioned. A non-panic reproducer never probes and keeps
  today's behavior exactly.
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
- **Reverse the routing too — record `crashed_halted_live` for a declared crash.** **verified:**
  ADR-0064 §2 binds `expected_crash_observed` to the reproduction verdict and to keeping the
  System reusable for the A/B pair, and `src/kdive/mcp/tools/lifecycle/runs/common.py` reads the
  outcome to render `expected_boot_failure_matched_line` and the capture disclosure. It would
  break the reproduction verdict to buy a transport that admitting the transport already buys.
- **Probe on any matched expectation, without the panic-signature gate.** **verified:** ADR-0233
  decision 3 and its "Treat the RSP probe as the crash signal" rejection record that an RSP
  connect halts the vCPU and may resume it on disconnect; `evaluate_expected_failure_after_ready`
  (ADR-0383) reaches `record_expected_crash` from the *ready* path, so an unpanicked guest is
  reachable there and an ungated probe could freeze it.
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
