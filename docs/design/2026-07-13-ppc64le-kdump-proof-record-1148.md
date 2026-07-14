# Proof record — ppc64le kdump capture under TCG (#1148)

Date: 2026-07-13
Issue: #1148 · Epic: #1139 · Spec: `2026-07-13-ppc64le-kdump-crashkernel-1148.md` · ADR-0346

> **Status: PENDING — the code (per-arch crashkernel default + host-side depmod) has landed and is
> CI-green; the blocking live capture run is not yet recorded here.** Per the issue owner there is
> no CONSTRAINED fallback for the capture itself: this record is completed only when a real ppc64le
> vmcore has been captured under TCG on the x86_64 host and retrieved with makedumpfile fields
> recorded. Until then #1148's acceptance criterion 5 is **not** met.

## What the live run must record (from the plan §Task 3)

The documented `live_stack` run force-crashes a provisioned ppc64le guest under TCG on the x86_64
host and captures + retrieves a vmcore. This record will capture:

- **Preconditions met:** a kdump-enabled ppc64le rootfs (kexec-tools + `kdump.service` + dracut
  kdump module — prepared via #1147 customization boot if the base image lacks it) and the guest's
  total RAM (≥2 GB, so a 512M reservation is honored and still leaves a bootable first kernel).
- **Host-side depmod, end-to-end:** the ppc64le KDUMP install completes and
  `/lib/modules/<ver>/modules.dep` is present in-guest — the cross-arch indexing fix (#1148,
  ADR-0346) exercised through the real writer, retiring #1146's CONSTRAINED depmod verdict.
- **Arch-default reservation (discriminating):** `crashkernel=512M` (not the `256M` sentinel
  profile token, and no per-install override) in the crashed guest's `/proc/cmdline`, plus the
  per-Run staged `<kernel>` and an `EM_PPC64` core — so the capture is provably this ppc64le
  guest's under the arch default.
- **Captured vmcore + makedumpfile fields:** the retrieved `-redacted` vmcore artifact and the
  makedumpfile-reported fields.
- **VMCOREINFO/fw_cfg verdict (§3):** whether the capture needed any pseries `<features>` device
  emission (hypothesis: none — kdump reads VMCOREINFO from `/proc/vmcore`, the `<vmcoreinfo>`
  device serves host_dump). The verdict is mirrored into ADR-0346's Live-proof outcome section.

## Result

_Pending the live run._
