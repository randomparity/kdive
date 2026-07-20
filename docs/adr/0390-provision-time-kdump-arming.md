# ADR 0390 — Provision-time kdump arming for a warm-own-kernel System

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-20
- **Deciders:** Maintainer (randomparity), Claude Code
- **Issue:** [#1319](https://github.com/randomparity/kdive/issues/1319)
- **Refines (does not supersede):** [ADR-0206](0206-modules-in-guest-shared-contract.md)
  (the local-libvirt install-lane kdump gate) and
  [ADR-0272](0272-provision-baseline-kernel-boot.md) (the baseline direct-kernel boot
  whose cmdline this extends).
- **Spec:** [`../superpowers/specs/2026-07-20-provision-arm-kdump-1319.md`](../superpowers/specs/2026-07-20-provision-arm-kdump-1319.md)

## Context

The native `live_vm` gate (epic #1289 / #1293) mints a warm-store-provisioned System,
force-crashes it, and expects a real `/var/crash/<ts>/vmcore`. The kdump acceptance test
`test_retrieve_kdump.py::test_live_vm_kdump_capture_arc_no_staging` fails because the
guest is **not kdump-armed**.

Under ADR-0206/0272, `crashkernel=` was "the install/boot lane's job": only
`runs.install` (an agent installing a kernel-under-test, sizing the reservation against
it) put `crashkernel=` on the cmdline, and the baseline provision boot deliberately never
carried it. But a **warm-own-kernel** System boots the rootfs's own kernel, whose modules
already ship in the rootfs — it never runs `runs.install`. So there is no lane that ever
reserves crash memory for it: setting the profile's `crashkernel` field selects
`capture_method=KDUMP` (so `vmcore.fetch method=kdump` is offered) but the boot cmdline
still lacks `crashkernel=`, so the kernel reserves nothing and kdump captures nothing.

The install-lane gate (`install.py`'s `kdump_env_absent`, which requires an injected
`modules_ref`/`initrd_ref`) does not fit this case either: the warm store produces no such
artifact because the rootfs already carries `/lib/modules`.

## Decision

Arm kdump at **provision time** for the warm-own-kernel case (issue Option A).

`render_domain_xml` appends `crashkernel=<size>` to the System's baseline boot cmdline when
the local-libvirt profile sets `crashkernel`, and appends `fadump=on` after it when
`debug.fadump` is set — mirroring the install lane's `system_required_cmdline` token order.
The reservation size is the profile's opaque `crashkernel` token verbatim (the booted
kernel is the arbiter of its grammar, as it already is on the install lane). A profile with
no `crashkernel` renders the unchanged `root=/dev/vda console=<dev> rw`, and the
transient customization-boot renderer is never armed.

This relaxes the ADR-0206/0272 invariant "`crashkernel` is never on the baseline boot" for
the warm-own-kernel case only. It is a relaxation of a **policy**, not a weakening of a
safety guard: the install lane's `kdump_env_absent` gate stays exactly as it is (it guards
the install-a-custom-kernel path, which this bypasses).

The in-guest half is already satisfied by the image build: a kdump-capable (`debug`) image
runs `systemctl enable kdump.service` and orders the `kdive-ready` marker
`After=kdump.service` (`_fedora_customize.py`, `rhel.py`), so a System provisioned from
such a warm rootfs arms kdump at first boot with no operator step. Provisioning the warm
store from a kdump-capable image is the operator's `KDIVE_WARM_STORE_IMAGE` choice.

`scripts/live-vm/mint-system.sh` — which mints the shared provisioned-family System — now
requests `crashkernel` in the profile (so the minted System is a real kdump System) and
provisions at 4 GiB (2 GiB + crashkernel cannot reach the readiness marker on the x86 warm
guest, proven live on runner-pdx).

## Consequences

- The kdump native-gate test becomes satisfiable on a warm-provisioned System without any
  manual pre-arming.
- A ppc64le fadump warm System is armed consistently (`crashkernel=` + `fadump=on`),
  avoiding a half-armed reservation.
- The baseline provision cmdline is no longer byte-identical for a crashkernel-bearing
  profile; the provisioning golden tests are updated to the armed cmdline.
- The profile `crashkernel` field — previously an inert KDUMP *signal*, now rendered
  verbatim into a boot `<cmdline>` — is validated at parse for cmdline-injection safety
  (no internal whitespace, printable, no `crashkernel=` prefix), the same `CrashkernelToken`
  rules the install lane already applies to its per-install override.
- No DB migration, no payload/schema change, no new tool or error category.

## Alternatives rejected

- **Warm store emits `modules_ref`; the gate runs `runs.install`** (issue Option B) —
  larger: it changes the warm-store contract and adds an install step to the mint path,
  to re-derive modules the rootfs already carries.
- **Skip the kdump test unless a pre-armed signal is set** (issue Option C) — the gate
  stops failing but kdump is never exercised unattended; the point of the native gate is
  real crash-capture coverage.
- **Weaken the install-lane `kdump_env_absent` gate** — unnecessary (the warm path never
  enters install) and it would let a genuinely mis-installed custom kernel arm kdump with
  no capture environment.
