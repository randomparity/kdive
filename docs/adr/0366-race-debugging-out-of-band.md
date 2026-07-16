# ADR 0366 — Resolve race investigation (#986) as out-of-band docs, not a non-stop gdbstub mode

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** kdive maintainers

## Context

Issue #986 asked for a **non-halting observation mode for race investigation**: a gdbstub
break halts every CPU (`src/kdive/providers/local_libvirt/lifecycle/xml.py`), which is correct
for a deterministic bug you stop at but wrong for a race — halting collapses the concurrent
timing that produces the failure, so the value you wanted to catch mid-flight never occurs
under the debugger. The issue offered two branches: evaluate a hardware-watchpoint / non-stop
attach mode for the QEMU gdbstub, **or** steer race work to drgn-live + tracepoints by
documentation.

This lands under the v0.3.0 release-readiness epic (#1199), Workstream D. That epic's governing
principle is *"a capability earns an MCP tool only if it is out-of-band"* — anything achievable
over the guest's root SSH stays a documented prompt pattern, not a tool. The SSH-equivalent
#998 tool proposals were closed on exactly that basis, and their **outcomes must still be
covered — by documentation**. #986 is where race investigation lands.

The existing surface already answers the race question out-of-band:

- **drgn-live** (`introspect.run` / `introspect.script`) reads a running kernel's memory and
  walks typed kernel structures **without stopping the CPU**, over the drgn-over-SSH transport
  that needs no credential provisioning (ADR-0085). It is the race-friendly path already
  documented in the introspect toolset guide.
- **Tracepoints / ftrace / bpftrace** run in-guest over root SSH through
  `/sys/kernel/{tracing,debug}` — low-overhead, non-halting, and needing no MCP tool because
  the guest is the agent's own root shell.
- The **reproducer loop** that provokes the race is the agent's own code run over root SSH
  (a stress/repeat-until-crash primitive is tracked separately as #984).

## Decision

Resolve #986 **as documentation**, not code. kdive ships **no** non-stop gdbstub mode. A new
race-debugging guide (`docs/operating/race-debugging.md`, linked from the operating index)
routes race investigation to the existing out-of-band primitives — drgn-live introspection and
in-guest tracepoints — and documents the *"the guest is yours as root — run commands / loop
reproducers over SSH"* contract, so every discarded #998 tool's outcome is reachable as a
prompt pattern.

The guide cites `control.force_crash` (NMI via libvirt) and `control.diagnostic_sysrq` (libvirt
`sendKey`) as the canonical **positive** examples of what an out-of-band tool looks like: they
earn their existence precisely because they act on a **dead or hung** guest that SSH cannot
reach. That is the line — anything doable over root SSH stays a documented pattern; only what
must work when SSH is gone warrants a tool.

## Consequences

- Race investigation has a single documented entry point that steers away from the
  timing-destroying gdbstub break and toward non-halting drgn-live + tracepoints.
- No new tool surface, no gdbstub code change, and no reprovision requirement for race work
  (drgn-live needs no `debug.gdbstub` provisioning).
- The out-of-band rule now has a written statement of its positive side (why `force_crash` /
  `diagnostic_sysrq` are tools) alongside its negative side (why race work is not).

## Alternatives considered

- **A non-stop / hardware-watchpoint gdbstub mode** (the #986 code branch). Rejected: it is
  net-new debugger surface and reprovision-bound, when drgn-live already reads live kernel
  state without halting and needs no gdbstub provisioning. Building it would duplicate an
  existing out-of-band capability and cross the epic's "tool only if out-of-band" line for an
  outcome root SSH + drgn already cover.
- **Leaving #986 open with no doc** (assume agents find drgn-live themselves). Rejected: the
  #998 closures explicitly moved these outcomes to documentation; an undocumented routing is a
  phantom resolution.
- **A dedicated tracepoint/ftrace MCP tool.** Rejected: it runs entirely inside a guest the
  agent owns as root — SSH-equivalent, so it fails the out-of-band test, exactly like the
  closed #998 proposals.
