# Scale SLOW_BUILD_TOOL_TIMEOUT_S off the worker host's KVM (#2397)

## Problem

`SLOW_BUILD_TOOL_TIMEOUT_S = 1800` (`providers/shared/build_timeouts.py`) bounds the rootfs build
tools. They drive a libguestfs appliance — a host-arch VM the worker boots on its own host — so
its speed is set by the worker host's `/dev/kvm`, not by any System's accelerator. Without KVM the
budget is unmeetable: #2383 measured in-guest `build-fs` failing at `virt-tar-out exceeded its
timeout {'timeout_s': 1800}`, deviation 1 of its proof record. ADR-0636 fixed the sibling budget.

## Scope

[ADR-0637](../../adr/0637-appliance-multiplier-is-provider-shared.md) decides the import direction
the issue names as its substance. Changes:

- New `providers/shared/appliance_budget.py` holds `host_appliance_multiplier()`, moved unchanged
  from `providers/local_libvirt/lifecycle/deadlines.py`, which re-exports it.
- `providers/shared/build_timeouts.py` gains `slow_build_tool_timeout_s() -> int`.
- Five module-level aliases of the constant are deleted; their 10 call sites in
  `local_libvirt/rootfs_build.py`, `local_libvirt/lifecycle/rootfs/customization_boot.py`, and
  `remote_libvirt/rootfs_build.py` call the function instead.
- #2383's proof record gains one line marking deviation 1 resolved.
- Out: `tcg_deadline_multiplier`, `_VIRT_CUSTOMIZE_TIMEOUT_S`, `overlay_customize.py` (ADR-0636,
  PR #2395); the fadump gating (#2398); remote-libvirt topology (ADR-0080/0092).

### Failure model

- **Actors and deployments:** the kdive worker process, on an operator's build host, running
  rootfs builds for local-libvirt and remote-libvirt. No other caller reaches these budgets.
- **Invariants at stake:** a KVM host's budget stays exactly 1800 s; `providers/shared` imports
  no local-libvirt implementation module; one operator knob moves both appliance budgets.
- **Accepted failure classes:** a hung build tool takes 5 h to surface on an emulated host, bounded
  by the job deadline above it — the cost of a meetable budget (ADR-0636). A `/dev/kvm` appearing
  or vanishing mid-build is not re-probed within a tool run. A host with KVM that is merely slow
  still fails; no budget fixes that.
- **Covered elsewhere:** the multiplier's value and single-knob constraint — ADR-0636; the
  `qemu:///session` vs system probe semantics — ADR-0352.

## Success

1. Every rootfs-build call site in both providers resolves its budget through
   `slow_build_tool_timeout_s()`; no module-level alias of the constant remains.
2. A worker host with KVM gets exactly 1800; one without gets 1800 x the configured multiplier.
3. `tests/providers/test_provider_boundaries.py` stays green.
4. Deviation 1 of the #2383 proof record is marked resolved.

## Validation

Green for every entry below: `uv run python -m pytest tests/providers -q`.

- `slow_build_tool_timeout_s()` both branches, int return — `focused-test`:
  `tests/providers/shared/test_build_timeouts.py`; red before the function exists.
- `host_appliance_multiplier` still importable from `deadlines` — `focused-test`: the existing
  `tests/providers/local_libvirt/test_deadlines.py` cases, unchanged; red without the re-export.
- Local acquire, repack, inject, seal budgets — `focused-test`:
  `tests/providers/local_libvirt/test_rootfs_build.py`, faking `subprocess.run` and asserting the
  scaled `timeout`; red against today's fixed 1800.
- Remote `virt-builder` budget — `focused-test`: `tests/providers/remote_libvirt/test_rootfs_build.py`,
  same shape; red against its current `== SLOW_BUILD_TOOL_TIMEOUT_S` assertion.
- Proof-record deviation line — `task-test-not-applicable`: prose no executable consumer reads.
