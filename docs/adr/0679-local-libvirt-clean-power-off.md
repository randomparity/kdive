# 0679 — Local-libvirt power-offs request a clean shutdown first

## Status

Accepted (2026-09-25)

Amends [ADR-0030](0030-install-boot-plane.md) §6 and
[ADR-0206](0206-modules-in-guest-shared-contract.md) §4: neither `boot()` nor the install
force-off hard-destroys a guest that can shut down.

> **Amended by [ADR-0681](0681-external-boot-clean-power-off.md) (#2780):** the external-boot
> preparation stops and KVM recovery stops now use this helper.

## Context

`runs.boot` (`_power_cycle`) and the module-injecting install (`force_off_if_active`) hard-kill a
running local-libvirt domain with `destroy()`. A guest killed that way loses page-cache writes it
has not flushed yet. Issue #2757 showed the cost: a System's first boot writes its SSH host keys
and cloud-init's once-per-instance markers, the next `runs.boot` destroys it about 8 s later, and
the keys survive as 0-byte files while the markers survive intact. sshd never starts again on that
System. Nothing in the chain is specific to one architecture.

## Decision

Both power-off sites share one helper that:

1. reads `domain.state()`. `SHUTOFF` needs nothing. `RUNNING` and `BLOCKED` can honour a request.
   `SHUTDOWN` is already stopping, and libvirt refuses a request in it, so it is waited for
   without one. Every other state (`PAUSED` — which includes a vCPU halted by a gdbstub client —
   `CRASHED`, `PMSUSPENDED`, `NOSTATE`) goes straight to `destroy()`;
2. otherwise calls `domain.shutdown()` with default flags, as `virsh shutdown` does: libvirt uses
   the guest agent when it answers and otherwise QEMU's `system_powerdown` (the ACPI power button
   on x86, an EPOW event on pseries). kdive neither selects nor requires the agent. It then
   polls `state()` once a second until `SHUTOFF` or a monotonic-clock deadline, re-sending the
   request every 10 s so a request that arrived before the guest's handler was listening is not
   lost;
3. falls back to `destroy()` when the wait expires or libvirt refuses the request;
4. logs which path ran and how long it took.

The wait is 60 s for a KVM guest, scaled by `tcg_deadline_multiplier(accel)` (ADR-0341). It is a
ceiling: a guest that powers off in 3 s costs 3 s. A `shutdown()` call that blocks inside
libvirt's guest-agent path (up to libvirt's 60 s agent shutdown timeout) counts against the
deadline but can overrun it by that one call. The bound is a module constant, not an operator
setting. `InstallRequest` gains an optional `accel` so the install-time force-off scales the same
way `boot()` does; `None` gets the TCG multiplier, as it does for `boot()`.

The ADR-0576 console truncate still runs after the domain is off and before `create()`.

## Consequences

- A guest that honours the request keeps its unflushed writes across a kdive power-cycle, which
  removes the #2757 root cause for `runs.boot` and module-injecting installs.
- A guest that is running but ignores the request (a panicked or hung kernel, a test kernel
  without an ACPI button or EPOW handler) now costs the full wait before `destroy()`: 60 s on KVM,
  600 s at the default TCG multiplier. This is routine, not rare: the `runs.boot` after a Run whose
  kernel panicked and hung pays it every time. The log line names this case.
- A guest's own shutdown path runs, so its shutdown messages reach the console before the
  truncate removes them, as the prior boot's messages always were. The truncate still waits for
  libvirt to report `SHUTOFF`, which it does only after QEMU has exited, so ADR-0576's ordering
  holds on the clean path as on the destroy path.
- The other local-libvirt `destroy()` sites (external boot sessions, the vmcore harvest, the
  customization boot) are unchanged.

## Considered & rejected

- **Do nothing; rely on ready-after-first-boot gating (#2771).** judgment: it protects only the
  first boot, while any later write still dies with a hard kill.
- **Require the guest agent (`VIR_DOMAIN_SHUTDOWN_GUEST_AGENT`).** judgment: the agent is not
  running early in a boot, which is when #2757 fires, and the operator excluded an agent shutdown
  mode; default flags still use the agent when it answers.
- **Make the wait an operator setting.** judgment: the TCG multiplier already covers the slow
  case, and a knob nobody has asked for is surface to document and test.
- **One request with no re-send.** judgment: a request sent before logind or the EPOW handler is
  up is dropped, and the wait then ends in the same destroy this ADR exists to avoid.
