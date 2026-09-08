# Proof record — ppc64le kdump capture under TCG (#1148)

> Historical design or proof for the dated change below. It is retained as decision evidence,
> not as current setup or API guidance. Use the [current documentation index](../README.md).

Date: 2026-07-13
Issue: #1148 · Epic: #1139 · Spec: `2026-07-13-ppc64le-kdump-crashkernel-1148.md` · ADR-0346

> **Status: PASS (2026-07-14).** A real ppc64le vmcore was captured under TCG on the x86_64 host
> and retrieved through the existing pipeline, with the makedumpfile-reported fields recorded
> below. #1148 acceptance criterion 5 is met.

## Environment

- Host: x86_64, `qemu-system-ppc64` present; libvirt advertises `ppc64le` (`accel=tcg`). Live
  stack (`scripts/live-stack/up.sh`) on build `gfc870c263` (this branch).
- Rootfs: `fedora-kdive-ready-44-ppc64le.qcow2` — kdump-enabled (provenance packages include
  `kexec-tools` 2.0.32, `makedumpfile` 1.7.9, `kdump-utils` 1.0.61; capabilities include `kdump`),
  built from the Fedora ppc64le Cloud base. **The kdump-userspace precondition was met by the
  published image** — no #1147 customization boot was needed for this run.
- Guest: `memory_mb=2048` (≥2 GB precondition met), `arch=ppc64le`, `machine=pseries`, `accel=tcg`.
- Uploaded bundle: the ADR-0343 combined tar (`boot/vmlinuz` = the ppc64le ELF,
  `lib/modules/6.19.10-300.fc44.ppc64le/`) + the matching `initramfs`, from `<REDACTED-HOME>/kdive-ppc-proof`.
- Driver: `tests/integration/test_live_stack.py::test_ppc64le_kdump_captures_a_vmcore_under_tcg`
  (`1 passed in 218.58s`). System/Run `9359253e-017a-4740-bb2a-3f008bae520c`.

## Result 1 — arch-default crashkernel=512M reached a real ppc64le KDUMP cmdline (PASS)

The KDUMP System's profile set the **sentinel** `crashkernel="256M"` (method signal only) with **no
per-install `crashkernel`**. The worker resolved the install cmdline (worker log):

```
install: run 9359253e… resolved cmdline 'console=hvc0 root=/dev/vda crashkernel=512M
  kdive_proof_token=[REDACTED]' (method kdump)
```

`crashkernel=512M` — the ppc64le **arch default** (ADR-0346 §1), not the `256M` sentinel — so the
per-arch default sized the reservation. The running domain's `<cmdline>` carried the same
`crashkernel=512M` (asserted in the `ppc64le-kdump:attribute` phase), and `console=hvc0` confirms
the pseries console.

## Result 2 — host-side depmod unblocked cross-arch module injection (PASS)

The KDUMP install fired `_RealGuestKernelWriter.inject`, whose module indexing now runs host-side
(`depmod -b`, ADR-0346 §2). The install step **succeeded** — no `depmod: Exec format error`,
retiring #1146's CONSTRAINED verdict (which recorded the in-guest ppc64le `depmod` failing under
the x86_64 libguestfs appliance). The live run also surfaced and fixed a real defect the unit
fakes missed: the initial `extractall(filter="data")` rejected the absolute-path `build`/`source`
symlinks every module tree carries (`AbsoluteLinkError`); the fix skips those link members with a
`data`-safe custom filter (commit `fc870c263`, regression test added).

## Result 3 — kdump captured a ppc64le vmcore, retrieved through the pipeline (PASS)

`control.force_crash` → the System reached `crashed` → `vmcore.fetch` (capture job) →
`vmcore.list`. Artifacts in the object store for the Run:

```
90463884  local/runs/9359253e…/vmcore-kdump            ← the captured core (~86 MiB)
     125  local/runs/9359253e…/vmcore-kdump-redacted   ← the surfaced ref (redaction contract)
```

Only the `-redacted` ref is surfaced by `vmcore.list`; the raw `vmcore-kdump` is never exposed
(redaction assertion held). The redacted artifact is a note — `[kdive] dmesg could not be extracted
from this core (kernel debuginfo required)` — expected, since this proof uploaded no DWARF vmlinux
(dmesg extraction is drgn-scoped, issues 10/11; not an AC here).

### makedumpfile fields (the AC)

The captured core is makedumpfile's default **KDUMP-compressed format** (not raw ELF). Its
`disk_dump_header` (makedumpfile 1.7.9) reports:

| field | value |
|-------|-------|
| signature | `KDUMP   ` |
| header_version | 6 |
| sysname | Linux |
| nodename | kdive |
| release | **6.19.10-300.fc44.ppc64le** |
| version | `#1 SMP PREEMPT_DYNAMIC Wed Mar 25 17:38:26 UTC 2026` |
| machine | **ppc64le** |
| size | 90463884 bytes (~86 MiB) |

`machine=ppc64le` + `release=…ppc64le` are the discriminating attribution — the core is *this*
ppc64le guest's, captured by its own kdump kernel and makedumpfile, at the arch-default 512M
reservation.

## Result 4 — pseries VMCOREINFO/fw_cfg verdict (ADR-0346 §3)

The kdump capture succeeded with **no `<features>` device emitted** on the pseries domain — the
`ppc64le-kdump:attribute` phase asserted `<vmcoreinfo` is absent from `virsh dumpxml`, and the
capture still produced a valid vmcore. This confirms the hypothesis: **kdump on pseries needs no
QEMU VMCOREINFO/fw_cfg device.** makedumpfile read VMCOREINFO from the crashed kernel's
`/proc/vmcore` ELF note, independent of any QEMU device. The x86-only `emit_acpi_features` gate
(ADR-0340) is correct for the kdump path; no `xml.py`/`arch_traits` change was required. The pseries
host_dump fw_cfg/device-tree question is a separate capture method, out of scope here. This retires
the epic's "pseries fw_cfg/VMCOREINFO device behavior (issue 9)" Known-unverified item.

## Verdict summary

| claim | verdict | evidence |
|-------|---------|----------|
| per-arch default sizes a real ppc64le KDUMP cmdline (512M, not 256M) | **PASS** | worker cmdline + domain `<cmdline>` |
| host-side depmod indexes a ppc64le module tree under the x86_64 appliance | **PASS** | KDUMP install succeeded; no Exec format error |
| kdump captures + retrieves a ppc64le vmcore under TCG | **PASS** | `vmcore-kdump` 86 MiB; makedumpfile header `machine=ppc64le` |
| pseries kdump needs no `<features>` VMCOREINFO device | **NO (none needed)** | no `<vmcoreinfo>` in domain XML; capture still succeeded |
