# `faultinject.set` and `faultinject.reset`: bounded guest fault arming (#3684)

Decision record: [ADR-0826](../../adr/0826-faultinject-set-reset-bounded-arming.md).
Parent: #918. Predecessors: #3682 (helper, ADR-0822), #3683 (inspect, ADR-0825).

## Problem

An agent can read fault-injection state but not set or clear it. A careless arm can lock the agent
out of the guest, so the controls must be bounded and self-exhausting.

## Scope

In: two MCP tools, two port methods and their local-libvirt implementation, shared bounds and
validation, the `faultinject.inspect` next-action change, exposure, tool-index, generated CLI
verbs, docs, coverage obligations, a live_stack tool cell, and a live proof on real x86_64
hardware with an armed-knob read. Out (unowned): the run-owned reproducer workflow (#919);
knobs beyond `failslab` and `fail_page_alloc`; other providers; free-form debugfs writes; the
helper's task-filter and gfp flags; fault injection in stock images or fixture kernels; bounding
guest stdout in the shared exec seam (#3696).

Ownership: no transition; the port from ADR-0825 gains two methods and `_live_ssh_argv` is reused.

## Contract

Both tools: contributor, mutating annotation, ready local-libvirt System, `system_id` UUID.

`faultinject.set(system_id, knob, probability, times, interval?, space?, verbose?, min_order?)`

| Parameter | Bound | Unit and meaning |
|---|---|---|
| knob | `failslab` or `fail_page_alloc` | the only allowlisted knobs |
| probability | 1..100 | percent chance an eligible allocation fails; 0 refused (use reset) |
| times | 1..1000000 | injected failures guest-wide before the kernel stops; -1 and 0 refused |
| interval | 1..1000000 | inject at most once per this many eligible calls; unset keeps the guest value |
| space | 0..1073741824 | bytes of allocation before injection can begin; unset keeps the guest value |
| verbose | 0..2 | kernel log verbosity of each injection; unset keeps the guest value |
| min_order | 0..10 | `fail_page_alloc` only: smallest page order that can fail; unset keeps it |

A violation is `configuration_error`, `data.reason` `unknown_knob` / `parameter_out_of_range` /
`parameter_not_applicable`, before any DB read of the System's guest state or any ssh call.
Recovery: send a value inside the stated range.

`faultinject.reset(system_id, knob?)` writes the helper's kernel defaults (probability 0, times 1,
interval 1, space 0, verbose 2, min-order 1) to the named knob, or to every present knob when
omitted.

Success `data`: `{"knobs": {"<knob>": {"present", "armed", <param>: int}}}` for the touched knob(s),
parsed from the helper's printed state. `suggested_next_actions`: set -> `faultinject.inspect`,
`faultinject.reset`; reset -> `faultinject.inspect`; inspect -> adds `faultinject.reset` when any
knob is armed.

Errors beyond ADR-0825's: `configuration_error` `reason=knob_absent` (named knob not in the kernel);
`reset` with both knobs absent is `fault_injection_unsupported`. A timeout after the argv started is
`transport_failure` and the guest state is unknown (call `faultinject.inspect`).

## Failure model

- Arm then lose the guest: `times` is finite, so the kernel stops injecting after at most
  1000000 failures; a reboot also clears it.
- Helper/bounds drift: a test parses the helper's `param_bounds` and compares it with the Python
  table.
- Partial write: the helper validates every pair first and writes `probability` last, so a failure
  never leaves injection armed half-applied.
- Lost reply: state unknown, documented; no retry past the connect phase.

## Tests

Provider: argv built per knob and parameter order, validation before run, exit-code mapping, strict
readback parse for a single knob, bounds table equals helper. Tool: both knobs set, unknown knob,
each bound edge and `times` -1/0, `min_order` on failslab, helper not run on rejection, reset one
and all, absent knob, missing support, denial below contributor, non-ready System, inspect next
actions. Registry: exposure, tool index, docs, generated verb, coverage obligations. Live: functional
cells in the system-tool cell lane and a real-hardware armed-read/set/readback/reset proof.
