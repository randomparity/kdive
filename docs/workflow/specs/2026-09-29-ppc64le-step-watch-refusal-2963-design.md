# Refuse single-step and watchpoints on ppc64le KVM (#2963) — design

Decision record: [ADR-0712](../../adr/0712-debug-capability-table-refuses-single-step-and-watchpoints.md).
Parent: #2942. Sibling that owns resume-time reclassification: #2964.

## Problem

On a ppc64le KVM System the pseries gdbstub cannot single-step or insert a hardware watchpoint
(#2739 proof record, sections 3-5). `debug.advance` modes `into`, `over` and `instruction` leave
the vCPU running and report `transport_stall` or `timed_out: True`. `debug.set_watchpoint`
arms a watchpoint that the next resume cannot insert.

## Design

1. New module `src/kdive/providers/shared/debug_common/gdbmi/policy/capabilities.py`:
   - `class DebugCapability(StrEnum)`: `SINGLE_STEP = "single_step"`,
     `HW_WATCHPOINT = "hw_watchpoint"`.
   - `_UNSUPPORTED: dict[tuple[str, str], frozenset[DebugCapability]]` with the one row
     `("ppc64le", "kvm")`.
   - `supports(arch: str | None, accel: str | None, capability: DebugCapability) -> bool`:
     `True` for an unknown arch; a `None` accel counts as `"kvm"` (ADR-0712 Decision 2);
     `False` only when the row names the capability.
2. `src/kdive/mcp/tools/debug/operations/runtime.py`:
   - `run_engine_op_with_resolver` takes `requires: CapabilityRequirement | None = None` and
     passes it to `_run_engine_op` (the registered tools use only this runner).
   - `CapabilityRequirement` is a frozen dataclass: `capability`, `code`, `detail`,
     `next_actions`, `data`.
   - After `_live_session` returns a session and before the runtime lookup, a set `requires`
     reads the System for `session.run_id` (`RUNS.get`, then `SYSTEMS.get`). It takes
     `arch = provisioning_profile.get("arch")` when that is a `str`, and `accel = system.accel`.
     When `supports(...)` is `False` it returns `ToolResponse.failure(session_id,
     NOT_IMPLEMENTED, detail=..., suggested_next_actions=..., data={"code", "arch", "accel",
     **data})`. A missing Run or System gives an unknown arch, so the op runs as before.
3. `execution.py`: `_register_debug_advance` passes a single-step requirement for modes `into`,
   `over` and `instruction` (`data={"mode": mode}`) and `None` for `out`. Code
   `single_step_unsupported`; next actions `debug.advance`, `debug.set_breakpoint`,
   `debug.continue`.
4. `watchpoints.py`: `_register_debug_set_watchpoint` passes an `HW_WATCHPOINT` requirement.
   Code `watchpoint_unsupported`; next actions `debug.set_breakpoint`, `debug.continue`.
5. Tool docstrings for `debug.advance` and `debug.set_watchpoint` name the refusal; the generated
   reference is regenerated with `just docs`. `docs/guide/toolsets/debug.md` gets one sentence.
6. Tests: `tests/mcp/debug/session_support.py` `seed_system` and `seed_live_session` take
   `accel`; the live smoke seeds `accel="kvm"`, the `render_domain_xml` default its domains
   use. Offline gate tests drive the registered tools through `_call_registered_debug_tool`.
   The `_UNSUPPORTED_ADVANCE_GROUPS` skip becomes an expected-refusal table: on ppc64le the
   single-step group stops once at the `vfs_read` breakpoint, clears it, and asserts
   `single_step_unsupported` with an unchanged `pc` for each of the three modes; the smoke
   asserts `watchpoint_unsupported`, zero listed watchpoints, and a successful
   `debug.read_registers`.

## Failure model

1. Actors and deployments: an authenticated contributor agent through MCP; local-libvirt and
   remote-libvirt Systems on x86_64 and ppc64le hosts.
2. Invariants and assets: a halted guest must stay halted when a step is refused; the public
   error codes and x86_64 behavior are published contracts.
3. Accepted failure classes:
   - A System with no arch runs the op as before; its resume-time failure is #2964's.
   - A ppc64le TCG guest on a local host not re-discovered since ADR-0338 (NULL `accel`) is
     refused; re-discovery records `tcg`.
   - A ppc64le KVM-PR System is refused without proof; kdive does not record HV versus PR.
   - A second gdb client or an engine-level caller that bypasses MCP is not gated.
   - `debug.continue` from a pc with an inserted breakpoint needs a gdb step-over; that path is
     not gated here and stays with #2964.
4. Covered elsewhere: resume-time reclassification of steps and watchpoint inserts (#2964);
   hardware breakpoints (follow-up, operator exclusion); stepping support in QEMU (upstream).

## Success

- On `("ppc64le", "kvm")`, `debug.advance` `into`/`over`/`instruction` and
  `debug.set_watchpoint` return the typed codes above and write no gdb/MI command.
- `out` and the ops registered without a requirement are unchanged; on x86_64 every op is
  unchanged. On ppc64le KVM the refusal precedes `debug.set_watchpoint` argument validation.
- Native ppc64le KVM-HV proof: `test_live_vm_debug_advance_modes` (both groups) and
  `test_live_vm_gdbmi_promoted_ops_smoke` pass with the refusal assertions.
