# Scale SLOW_BUILD_TOOL_TIMEOUT_S off the worker host's KVM (#2397)

## Problem

`SLOW_BUILD_TOOL_TIMEOUT_S = 1800` (`providers/shared/build_timeouts.py`) bounds the rootfs build
tools. They drive a libguestfs appliance — a host-arch VM the worker boots on its own host — so its
speed is set by the worker host's `/dev/kvm`, not by any System's accelerator. Without KVM the budget
is unmeetable: #2383 measured in-guest `build-fs` failing at `virt-tar-out exceeded its timeout
{'timeout_s': 1800}`, deviation 1 of its proof record. ADR-0636 fixed only the sibling budget.

## Scope

[ADR-0637](../../adr/0637-shared-build-budget-resolves-its-multiplier.md) decides the import
direction the issue names as its substance. Changes:

- `providers/shared/build_timeouts.py` gains `slow_build_tool_timeout_s(*, kvm_present=None) -> int`,
  resolving the ADR-0352 KVM probe and `LIBVIRT_TCG_DEADLINE_MULTIPLIER` itself.
- Five module-level aliases of the constant are deleted; their 10 call sites in
  `local_libvirt/rootfs_build.py`, `local_libvirt/lifecycle/rootfs/customization_boot.py`, and
  `remote_libvirt/rootfs_build.py` call the function instead.
- #2383's proof record gains one line marking deviation 1 resolved.
- Nothing moves. Out: `deadlines.py`, `host_appliance_multiplier`, `tcg_deadline_multiplier`,
  `_VIRT_CUSTOMIZE_TIMEOUT_S`, `overlay_customize.py` (ADR-0636, PR #2395); the fadump gating
  (#2398); remote-libvirt topology (ADR-0080/0092).

### Failure model

- **Actors and deployments:** the kdive worker process, on an operator's build host, running
  rootfs builds for local-libvirt and remote-libvirt. No other caller reaches these budgets.
- **Invariants at stake:** a KVM host's budget stays exactly 1800 s; `providers/shared` imports no
  local-libvirt implementation module; one operator knob still moves both appliance budgets.
- **Accepted failure classes:** a hung build tool occupies the worker for 5 h on an emulated host
  and nothing above it terminates the run — the job lease is a per-heartbeat limit, not a job
  runtime limit (`jobs/queue.py`); that is the price of a meetable budget. A `/dev/kvm` appearing
  or vanishing mid-build is not re-probed within a tool run.
- **Covered elsewhere:** the multiplier's value and one-knob rule — ADR-0636; probe URI semantics — ADR-0352.

## Success

1. Every rootfs-build call site in both providers resolves its budget through
   `slow_build_tool_timeout_s()`; no module-level alias of the constant remains.
2. A worker host with KVM gets exactly 1800; one without gets 1800 x the configured multiplier.
3. `tests/providers/test_provider_boundaries.py` stays green.
4. Deviation 1 of the #2383 proof record is marked resolved.

## Validation

Green for every entry: `uv run python -m pytest tests/providers -q`, then `just ci` for criterion 5.

- Success 2, both branches and int return — `focused-test`:
  `tests/providers/shared/test_build_timeouts.py`, injecting `kvm_present` so the result does not
  depend on the runner's own `/dev/kvm`; red before the function exists.
- Success 1, local acquire/repack/inject/seal and remote `virt-builder` budgets — `focused-test`:
  `tests/providers/{local_libvirt,remote_libvirt}/test_rootfs_build.py`, faking `subprocess.run` and
  stubbing each module's imported `slow_build_tool_timeout_s`; red today, where each site passes the alias.
- Success 1, no alias survives — `focused-test`: a guard in `tests/providers/shared/test_build_timeouts.py`
  asserting no consumer module binds `SLOW_BUILD_TOOL_TIMEOUT_S` at module level; red against all five today.
- Success 3 — `focused-test`: `tests/providers/test_provider_boundaries.py`, unchanged; red if the
  rejected `local_libvirt.lifecycle.deadlines` import is added to `shared/`.
- Success 4 — `task-test-not-applicable`: prose no executable consumer reads.
