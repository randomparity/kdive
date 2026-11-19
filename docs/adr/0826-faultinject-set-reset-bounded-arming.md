# 0826 — `faultinject.set` and `faultinject.reset` arm only finite, validated faults

## Status

Accepted (2026-11-19)

Extends [ADR-0825](0825-faultinject-inspect-via-optional-local-libvirt-port.md) (the optional
`fault_injection` port and the readback shape) and uses the reviewed in-guest program of
[ADR-0822](0822-guest-exec-allowlist-adds-kdive-faultinject.md). Amends neither.

## Context

#3684 adds the mutating half of #918. A knob that is armed can fail allocations in every task in
the guest, including `sshd`. The helper accepts `times` from -1 (unlimited) to 1000000, so a caller
that arms an unlimited fault can lock itself out of the guest and lose the ability to run `reset`.
Four choices have viable alternatives: the role, where validation lives, whether unlimited arming
is allowed, and how a lost reply is reported.

## Decision

- **Two tools, both at contributor.** `faultinject.set(system_id, knob, probability, times,
  interval?, space?, verbose?, min_order?)` and `faultinject.reset(system_id, knob?)` carry the
  mutating annotation and the `_CONTRIBUTOR` exposure scope, the same gate as the other tools that
  write guest state for a live debug (`introspect.script`, `control.diagnostic_sysrq`). Both need a
  `ready` System, the loopback SSH forward and the bootstrap key, exactly as `faultinject.inspect`.
- **The port grows two methods.** `FaultInjectionInspector` gains `set_knob` and `reset`, still
  local-libvirt only and still `None` elsewhere (`capability_unsupported`). The Protocol keeps its
  name; every method runs one fixed `kdive-faultinject` argv.
- **One bounds table, validated twice, before any guest call.** `FAULT_PARAMETER_BOUNDS` in
  `providers/ports/fault_injection.py` mirrors the helper's table; a test parses the helper script
  and fails on drift. `validate_set_request` runs at the tool boundary and again in the provider
  before argv is built. A rejection is `configuration_error` with `data.reason` one of
  `unknown_knob` (with `accepted_values`), `parameter_out_of_range` (with `parameter`, `minimum`,
  `maximum`) or `parameter_not_applicable` (`min_order` on `failslab`), and the helper is not run.
- **Arming needs a finite `times`.** `set` requires `probability` 1..100 and `times` 1..1000000. `-1`
  (unlimited) and `0` (exhausted) are refused, and so is `probability` 0, which would only disarm
  (`reset` does that). `times` counts injected failures across the whole guest, not seconds or
  calls: the kernel stops injecting after that many, with no clock and no further call, so a guest
  that can no longer run `reset` stops failing allocations once that many failures were injected
  (slow at a low probability or a high interval), or on reboot (debugfs state does not survive one). The optional `interval`, `space`, `verbose` and `min_order`
  are written only when given; unset ones keep their current guest value, which the readback shows.
- **Readback is the helper's own printed state**, parsed with the strict rules of ADR-0825 and
  shaped as `data.knobs.<knob>{present, armed, <parameters>}` for the knob(s) the call touched.
  `set` and `reset` suggest `faultinject.inspect`; `set` also suggests `faultinject.reset`.
  `faultinject.inspect` adds `faultinject.reset` to its `suggested_next_actions` when any knob is
  armed.
- **Error mapping extends ADR-0825's.** Helper exit 3 is `debugfs_not_mounted`; exit 127 is
  `helper_not_installed`; exit 4 on a named knob is the new `configuration_error`
  `reason=knob_absent` (that knob needs its CONFIG option); `reset` with no knob skips an absent
  knob and answers `fault_injection_unsupported` only when both are absent. Exit 255 is
  `transport_failure`; any other exit or invalid reply is `infrastructure_failure`.
- **A failed write can still have happened.** A timeout or a lost reply after the argv started
  leaves the guest state unknown; the docstring says to call `faultinject.inspect` (and
  `faultinject.reset` when it shows an armed knob) before assuming either outcome. Connect-phase ssh
  failures are retried as for `read` because the helper had not run.
- **Validation runs before the System lookup.** The knob list and bounds are static and the same
  for every caller, so an early rejection discloses nothing about a System.
- **Writes are audited, including ones with an unknown outcome.** A successful call writes one
  audit row (`tool`, `systems`, knob and parameters). A transport or infrastructure failure after
  the command may have started writes a row marked `outcome: unknown`; a configuration error
  writes none.
- **No server-side lock.** Writes are last-writer-wins per guest; two concurrent calls both
  succeed and each reports the state its own helper printed.

## Consequences

- A contributor can make a guest fail allocations for at most 1000000 injected failures per `set`;
  repeated calls can re-arm, which is the caller's authority over its own System.
- `reset` writes the kernel defaults, not the values found before `set` (ADR-0822).
- The Protocol name `FaultInjectionInspector` now covers writes; a rename was rejected as churn
  with no behavior change.
- The helper's `-1` and `0` for `times` stay legal in the guest and in `inspect` output; only the
  tool refuses to write them.

## Considered & rejected

- **Operator role.** judgment: the other in-guest mutating debug tools sit at contributor, a
  fault is bounded and per-System, and `reset` and a reboot are always available to the same
  caller.
- **Allow `times=-1` with a wall-clock timer that resets it.** judgment: the server would need a
  durable job to disarm, and the case it exists for is a guest that cannot be reached.
- **Pydantic `ge`/`le` bounds on the wrapper.** verified: a schema violation raises a raw
  `ValidationError` rather than the uniform envelope (the rationale in `domain/labels.py`); the
  bounds are stated in `Field` text and enforced in code.
- **A worker job.** judgment: one bounded round trip, as for ADR-0825.
- **Enable fault injection in stock images or fixture kernels.** Excluded by the charter; a
  kernel built with `CONFIG_FAULT_INJECTION` is the caller's.
