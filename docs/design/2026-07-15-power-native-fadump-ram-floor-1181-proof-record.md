# Proof record — native-POWER fadump RAM floor (#1181)

Date: 2026-07-15
Issue: #1181 · Epic: #1139 · ADR-0363 · Prior: #1156 / ADR-0355 (native KVM-HV validation),
#1151 / ADR-0349 (fadump opt-in)

> **Status: HOST FADUMP-READY + CODE FIX LANDED; end-to-end native crash→capture at the 4 GiB
> floor PENDING a fully-provisioned POWER live-stack.** The root cause (fadump fails run-readiness
> at 2 GiB because it reserves a boot-memory region on top of `crashkernel`) is fixed by a fadump
> RAM floor of 4096 MiB enforced at admission (ADR-0363). The target POWER10 dev VM advertises the
> QEMU floor that gates fadump (10.2.1 ≥ 10.2, ADR-0349) and has KVM-HV, so it *would* admit and
> boot the fadump profile; but the VM is not yet provisioned with the ppc64le rootfs fixture,
> kernel bundle, OIDC issuer, and live-stack processes the crash→capture proof drives, so the
> end-to-end capture was not executed in this change. The repro to complete it once the host is
> provisioned is below.

## What this change does (and how it maps to acceptance)

#1181 acceptance: *a native-POWER fadump crash→capture completes and retrieves a vmcore at the
chosen memory floor, re-runnable via the runbook §7.*

The design fork (issue "Candidate fixes") is resolved to **Fix 1 — a fadump guest-RAM floor**, not
a readiness-deadline accommodation (ADR-0363 §Rejected alternatives). Evidence: on the #1156 native
KVM-HV run the kdump variant of the *same guest/kernel/bundle* passed run-readiness at 2 GiB while
the `fadump=on` variant failed it. fadump reserves a boot-memory region (the region the production
kernel is re-launched into) on top of the `crashkernel` reservation, so the shortfall is memory,
not time — a slower deadline cannot recover a guest that has too little RAM to reach userspace
readiness at all.

The fix:

- `FADUMP_MIN_MEMORY_MB = 4096` and a `ProvisioningProfile` validator
  (`_require_fadump_memory_floor`) reject a fadump profile whose concrete `memory_mb` is below the
  floor with `CONFIGURATION_ERROR`, beside the ADR-0349 ppc64le/reservation preconditions. Enforced
  on the reconciled (booted) size, so a shape-sized `memory_gb=2` fadump allocation is rejected at
  `systems.provision`/`systems.define` (pre-capacity-commit), not after a failed boot.
- The #1181 native-POWER proof profile (`test_live_stack.py::test_ppc64le_fadump_captures_a_vmcore_under_tcg`)
  provisions at 4096 MiB with a paired `allocations.request` of `memory_gb=4`.

Verified by unit tests (`tests/profiles/test_provisioning.py`): under-floor fadump rejected,
at-floor accepted, floor deferred when `memory_mb` is omitted (the shape-sized lane). `just ci` is
green.

## Target host — fadump readiness confirmed live

Probed 2026-07-15 over `ssh -p 2223 dave@192.168.2.8`:

| | |
|---|---|
| host | POWER10 dev VM, `ppc64le`, 32 cores / 31 GiB RAM |
| virt | `qemu-system-ppc64` **10.2.1** (Debian 1:10.2.1+ds-1ubuntu3.1), libvirt 12.0, `/dev/kvm` present |
| fadump gate | QEMU 10.2.1 ≥ the ADR-0349 `PSERIES_FADUMP_QEMU_FLOOR` (10, 2) → `detect_pseries_fadump` = SUPPORTED |
| accel | `/dev/kvm` present → a ppc64le guest resolves `accel=kvm` (native, KVM-HV) |

So admission would accept the fadump profile on this host (the fail-closed ADR-0349 host gate
passes) and boot it under KVM-HV — the exact conditions the #1156 record established for the kdump
spine.

## Why the end-to-end capture is pending

The POWER10 dev VM is **not** provisioned with the live-stack the crash→capture proof drives.
Confirmed absent on the host: `/var/lib/kdive` (no rootfs fixture, no `bundle-ppc64le`), the guestfs
Python binding in the venv, a ppc64le kernel tree, and any running server/worker/reconciler or
OIDC issuer. Standing these up is the full runbook §0–§6: install guestfs, `build-fs` a
`fedora-kdive-ready-44-ppc64le` fixture, extract its kernel bundle, build/run the native OIDC
issuer, and bring up Postgres + MinIO + server/worker/reconciler — a multi-hour, multi-dependency
bootstrap not completed in this change.

## Repro to complete the capture (once the host is provisioned)

Follow `docs/operating/runbooks/power-host-bringup.md` §0–§6 to a ready host, then §7:

```bash
cd ~/src/kdive
set -a; source scripts/live-stack/env.sh
export KDIVE_GUEST_IMAGE_PPC64LE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2
export KDIVE_PPC64LE_BUNDLE=/var/lib/kdive/bundle-ppc64le
export KDIVE_KERNEL_SRC=/home/$USER/src/linux
set +a
uv run python -m pytest -m live_vm_tcg -o addopts="" -q -rA \
  -k test_ppc64le_fadump_captures_a_vmcore_under_tcg
```

Expected: with the 4 GiB floor, the guest reaches run-readiness under `fadump=on`; `control.force_crash`
panics it; fadump's memory-preserving reboot yields `/proc/vmcore`; `vmcore.fetch` harvests it under
the `vmcore-fadump` key. The `<cmdline>` assertions (`fadump=on`, `crashkernel=512M`) are unchanged
from the #1151 driver.
