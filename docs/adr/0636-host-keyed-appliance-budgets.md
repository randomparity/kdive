# 0636 — Host-side libguestfs budgets scale off the worker host, not the System

## Status

Accepted (2026-09-09)

- **Issue:** #2383

## Context

[ADR-0341](0341-tcg-deadline-scaling.md) established that a TCG guest executes about an order of
magnitude slower than a KVM one, and put a single multiplier —
`KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`, default `10.0` — behind `tcg_deadline_multiplier(accel)`,
keyed off the System's persisted `accel` fact. That key is right for what it governs: boot
readiness, where the thing being waited on is the System's own guest.

Provisioning also runs libguestfs tools on the worker, and those got fixed constants instead:
`_VIRT_CUSTOMIZE_TIMEOUT_S = 5 * 60` (`overlay_customize.py`) bounds the
`virt-customize --ssh-inject` that writes each System's bootstrap key (ADR-0289), and
`SLOW_BUILD_TOOL_TIMEOUT_S = 30 * 60` (`providers/shared/build_timeouts.py`) bounds the rootfs
build tools. Neither scales.

The 300 s constant is unmeetable on any host without usable `/dev/kvm`. Measured for #2383 on an
emulated-POWER host (Fedora 44 ppc64le guest, `IBM pSeries (emulated by qemu)`, QEMU 10.2.2, no
`/dev/kvm`), one injection against a Fedora 44 ppc64le overlay:

```
rc=0 elapsed=1474s
[ 734.6] SSH key inject: root
[ 894.4] SELinux relabelling
[1467.0] Finishing off
```

It succeeds; it is ~4.9x over budget. In the stack that surfaced as
`subprocess.TimeoutExpired` → `CategorizedError: virt-customize failed to inject the per-System
bootstrap key` → `libvirt failed to define/start the domain`, so no ppc64le System reached
`ready` and all three of #2383's live criteria were unreachable. A warm appliance cache does not
rescue it: inject (~160 s) plus the SELinux relabel (~573 s) alone is ~730 s.

The System's accel is the wrong key for this. A libguestfs appliance is not the System — it is a
*host-arch* VM the worker boots on its own host to edit a disk. The two facts are independent: a
ppc64le System under TCG on an x86_64 KVM host still gets a fast appliance, which is why the
hosted `live_vm_tcg` tier has never hit this, while any System on a host without `/dev/kvm` gets
an emulated one. Keying appliance budgets off `accel` would have scaled the fast case and missed
the slow one exactly backwards.

## Decision

Host-side libguestfs budgets scale off the **worker host's** KVM availability, guest-execution
deadlines continue to scale off the **System's** `accel`, and both use ADR-0341's one multiplier.

`host_appliance_multiplier()` joins `tcg_deadline_multiplier()` in
`providers/local_libvirt/lifecycle/deadlines.py`, reusing the existing
`kvm_probe_for_uri` (ADR-0352) — which already knows that `qemu:///session` means worker-uid
openability and any other URI means presence — and delegating to `tcg_deadline_multiplier` so the
factor stays defined once. A host with KVM is unscaled, so the fast path is byte-identical.

Only `_VIRT_CUSTOMIZE_TIMEOUT_S` adopts it here. `SLOW_BUILD_TOOL_TIMEOUT_S` has the same defect
and reaches only rootfs *build* paths, which #2383 does not exercise; it is recorded as a
follow-up rather than changed unexercised.

## Consequences

An emulated host gets 3000 s for one injection instead of 300 s, which the measured 1474 s fits
with margin. Operators already tuning `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` move both budgets
with one knob, and cannot tune them apart — acceptable while both express the same "no hardware
acceleration" penalty, and the reason the setting's help text now has to describe two keys.

A genuinely hung `virt-customize` on an emulated host now takes 50 minutes to surface instead of
5. That is the cost of the budget being meetable at all, and it is bounded by the job deadline
above it.

`SLOW_BUILD_TOOL_TIMEOUT_S` stays unscaled, so in-guest `build-fs` on an emulated host still
fails at `virt-tar-out exceeded its timeout {'timeout_s': 1800}`. #2383 worked around that by
building the image on an x86_64 host; the follow-up owns the fix.

The provider now imports `kdive.diagnostics.contributions.guest_arch_accel`. That direction is
already established (`providers/assembly/diagnostics.py`, `providers/remote_libvirt/diagnostics/`)
and does not cross the boundary ADR-0352 guards, which is that diagnostics must not import
`providers.local_libvirt.*`.

## Considered & rejected

- **Key appliance budgets off the System's `accel`, reusing `tcg_deadline_multiplier` directly.**
  verified: inverted on both real configurations — the hosted `live_vm_tcg` tier provisions
  ppc64le/`accel=tcg` Systems on x86_64 KVM runners, where the appliance is KVM-fast and would be
  needlessly scaled 10x, while #2383's host registered `guest_arches.ppc64le.accel=tcg` *and* has
  no `/dev/kvm`, so the slow case is the one the key cannot distinguish.
- **Raise the fixed constant to a larger fixed number.** judgment: it would have to clear the
  slowest emulated host, so every KVM host inherits a timeout that no longer bounds anything.
- **Add a separate `KDIVE_LIBVIRT_APPLIANCE_TIMEOUT_MULTIPLIER` setting.** judgment: a second knob
  for the same physical cause, with no evidence yet that an operator needs to move the two apart.
  Splitting later is cheap; un-shipping a setting is not.
- **Make `_VIRT_CUSTOMIZE_TIMEOUT_S` a plain `Setting` the operator sets per host.** verified: the
  fixed live-worker gate execs the worker from an environment allowlist (ADR-0621), so a new
  `KDIVE_*` variable does not reach the worker without also extending the lifecycle request
  contract — and it would push a host fact onto operators that the worker can observe itself.
- **Skip the SELinux relabel with `virt-customize --no-selinux-relabel`.** verified: the relabel is
  573 s of the measured 1474 s, so it does not close the gap on its own, and dropping it leaves a
  guest whose injected `/root/.ssh/authorized_keys` is mislabeled — sshd then refuses the key the
  System was provisioned with.
