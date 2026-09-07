# Proof record — native-POWER fadump RAM floor (#1181)

Date: 2026-07-15
Issue: #1181 · Epic: #1139 · ADR-0363 · Prior: #1156 / ADR-0355 (native KVM-HV validation),
#1151 / ADR-0349 (fadump opt-in) · Completed: #1204

> **Status: PASSED.** Native-POWER fadump crash→capture at the 4 GiB floor proved on
> ltcwspoon18 (Ubuntu 26.04.1 LTS ppc64le, POWER10, QEMU 10.2.1 KVM-HV) on 2026-09-07.
> `test_ppc64le_fadump_captures_a_vmcore_under_tcg` **PASSED** in 5:41.

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

## Target host

| | |
|---|---|
| host | ltcwspoon18 — POWER10, Ubuntu 26.04.1 LTS, `ppc64le` |
| virt | `qemu-system-ppc64le` **10.2.1** (Debian 1:10.2.1+ds-1ubuntu3.2), libvirt, `/dev/kvm` present (KVM-HV) |
| fadump gate | QEMU 10.2.1 ≥ the ADR-0349 `PSERIES_FADUMP_QEMU_FLOOR` (10, 2) → `detect_pseries_fadump` = SUPPORTED |
| accel | `/dev/kvm` present → ppc64le guest resolves `accel=kvm` (native KVM-HV) |
| kernel bundle | Fedora 44 ppc64le, kernel 6.19.10-300.fc44.ppc64le |
| guest image | `fedora-kdive-ready-44-ppc64le.qcow2` |

## Live-run evidence (2026-09-07)

```
ppc64le-fadump:provision: t+0s provisioning
ppc64le-fadump:provision: t+32s ready
ppc64le-fadump:crash: t+0s ready
ppc64le-fadump:crash: t+2s crashed
PASSED tests/integration/test_live_stack.py::test_ppc64le_fadump_captures_a_vmcore_under_tcg
1 passed, 18175 deselected, 4 warnings in 341.20s (0:05:41)
```

Key observations from the guest console:

- Boot 1: `rtas fadump: Registration is successful!` — fadump registered under KVM-HV, 512 MiB
  reservation at `0x20000000`.
- `force_crash` → guest rebooted in 2 s into capture kernel.
- Boot 2 (capture): `fadump: Firmware-assisted dump is active.` — firmware preserved 3584 MiB
  of crash memory at `0x20000000`; kernel updated cmdline with `nr_cpus=16 numa=off cgroup_disable=memory`.
- `fadump-capture.service` detected `/proc/vmcore`, ran `makedumpfile -F -l -d 31`, wrote
  `vmcore-fadump` to the guest overlay's `/var/crash/`, then powered off.
- Worker harvested the vmcore from the overlay; `vmcore-fadump` and `vmcore-fadump-redacted`
  published; `capture_vmcore` job `succeeded`.

## Bug fixes landed alongside this proof (PR #1204)

Three bugs surfaced during the native-POWER run and were fixed in `feat/ppc64le-live-proof-1204`:

**1. ppc64le ELF banner scan too narrow (`validation.py`)**

`_boot_release` read only the first `_EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES` (16 MiB) of
the ppc64le ELF boot member to locate the `"Linux version "` banner. Real Fedora 44 ppc64le
kernels place the banner at ~27 MiB into the stripped ELF (past the 16 MiB window), so
`runs.complete_build` rejected every upload with `"decoded boot/vmlinuz has no bounded
Linux release banner"`. Fixed by replacing the single read with a chunked scan bounded by
`_EXTERNAL_BOOT_DECODED_KERNEL_MAX_BYTES`, with an overlap buffer across chunk boundaries.
Regression test: `test_ppc64le_elf_boot_member_validates_when_banner_is_past_chunk_boundary`.

**2. `kernel.tar.gz` layout exceeded 128 MiB scan cap**

The Fedora 44 bundle's `kernel.tar.gz` included a duplicate `vmlinuz` copy under
`lib/modules/<rel>/vmlinuz` (63 MiB), pushing the first `.ko.xz` file past the
`_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB) scan bound before a kernel-module member could be
seen. The bundle was rebuilt with `--exclude='lib/modules/*/vmlinuz'` so `.ko.xz` files
appear at ~71 MiB (within the 128 MiB cap).

**3. fadump capture service missing from guest image**

`kdump.service` attempts to rebuild the fadump initrd in the capture kernel (second boot),
which fails in the kdive-supplied initrd environment. Without a working capture mechanism the
guest never writes `/var/crash/vmcore` and never powers off, causing the 120 s worker
timeout. Fixed by installing `fadump-capture.service` into the guest rootfs: it runs early
in boot, checks for `/proc/vmcore`, invokes `makedumpfile` when present, and calls
`poweroff -f`.

**4. Raw-vmcore leak assertion false-positive for large cores**

The inline `assert all(not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs)`
check fails for large vmcores: `artifacts.get` always adds a presigned `download_uri`, whose
query string follows the `-redacted` path segment, so the URL does not end with `-redacted`
even when the artifact is correctly redacted. Replaced with `raw_vmcore_refs(refs)` (which
uses `urlsplit` to check only the URL path) in the fadump, ppc64le-kdump, and x86 proof
assertions. (The `raw_vmcore_refs` docstring already cited this exact issue as #1610 from the
first ppc64le live run; the three proof tests had not yet adopted it.)

## Repro

Follow `docs/operating/runbooks/power-host-bringup.md` §0–§6 to a ready POWER10 KVM host, then §7:

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
the `vmcore-fadump` key. The `<cmdline>` assertions (`fadump=on`, `crashkernel=512M`) verify the
boot path.
