# Local-libvirt kdump: stage the from-source kernel into the guest `/boot` (#666)

- **Date:** 2026-06-21
- **Issue:** [#666](https://github.com/randomparity/kdive/issues/666)
- **ADR:** [ADR-0207](../../adr/0207-local-libvirt-stage-kernel-into-guest-boot.md)
  (refines [ADR-0206](../../adr/0206-modules-in-guest-shared-contract.md) §4)
- **Status:** Approved (design)

## Problem

ADR-0206 made the local-libvirt from-source kdump path inject `/lib/modules/<ver>` into the
per-System qcow2 overlay so the guest's `kdumpctl` can build a crash initramfs against the
custom kernel. A live end-to-end run on a real KVM host, driven entirely through the MCP tool
surface, confirmed the module-injection chain works — but the in-guest kdump **does not arm**:

- `control.force_crash` produces no vmcore.
- `vmcore.fetch method=kdump` fails with `readiness_failure` ("no complete core appeared").
- Post-run, read-only inspection of the overlay shows `/lib/modules/7.0.0` **present** (the
  ADR-0206 injection worked) but `/var/crash` **empty** and `/boot` containing only the base
  image's distro kernels (`vmlinuz-6.17…`, `vmlinuz-6.18…`) — **no `vmlinuz-7.0.0`**.

### Root cause

Local-libvirt boots `direct-kernel`: the running kernel is supplied to the domain by libvirt
as the `<kernel>` element (a host-staged `bzImage`), not installed into the guest filesystem.
A standard in-guest kdump arms by kexec-loading a crash kernel from `/boot/vmlinuz-$(uname -r)`.
Under direct-kernel boot `uname -r` is the from-source version (`7.0.0`), but the guest `/boot`
has no `vmlinuz-7.0.0`, so `kdumpctl` has no crash kernel to load — even with
`/lib/modules/7.0.0` injected and `kdump.service` enabled. Module injection alone cannot arm
in-guest kdump under direct-kernel boot.

**Named dependency:** this fix assumes the shipped guest image's `kdumpctl` resolves the crash
kernel by the bare path `/boot/vmlinuz-$(uname -r)` (the mechanism the #666 live run observed),
**not** via `grubby`/BLS (`/boot/loader/entries/*.conf`) — which under direct-kernel boot has no
entry and no grub. If a live run shows `kdumpctl` consulting BLS/`grubby` instead, staging the
bare path is necessary but not sufficient, and the contingency is a named follow-up: pin
`KDUMP_KERNELVER` in the guest's `/etc/sysconfig/kdump` and/or write a BLS entry for the
from-source kernel. That contingency is out of scope here and must not be assumed away — CI
cannot detect it (see Acceptance).

This is the second of the two gaps the live run found. The first (Fedora ships `kdump.service`
disabled; the run's image predated the rootfs builder's `systemctl enable kdump.service`) is an
**image-level** gap addressed by rebuilding the guest image with current code, and is out of
scope here. This spec is the **code** gap: the from-source kernel image is never placed in the
guest `/boot`.

### What we already have at install time

`install()` already fetches `kernel_ref` to the per-Run staging path
`{staging}/{system_id}/{run_id}/kernel` to point the direct-kernel `<kernel>` element at it. So
the kernel bytes are on the worker filesystem before module injection runs — the fix needs no
new build artifact and no new object-store fetch, only to land that same image inside the guest
overlay. The version `<ver>` is likewise already recovered from the modules tarball
(`_RealGuestModuleWriter._read_release`).

## Decision (summary; full rationale in ADR-0207)

On the from-source KDUMP lane (`method is KDUMP` and `modules_ref is not None`), the install
plane stages the from-source kernel image into the guest overlay at `/boot/vmlinuz-<ver>` in
the **same rw libguestfs session** that injects `/lib/modules/<ver>`, where `<ver>` is the
version read from the modules tarball. The guest's `kdumpctl`/`kdump.service` then finds the
crash kernel and builds + kexec-loads the crash environment.

- **One seam, one mount.** ADR-0206's `GuestModuleWriter` is renamed `GuestKernelWriter` and
  its `inject` takes the kernel image path. The single rw mount clobbers + extracts modules,
  runs `depmod`, `mkdir -p /boot`, then uploads the kernel to `/boot/vmlinuz-<ver>`. Modules
  and the kernel that must match them are written together in one force-off window.
- **Version is the modules-tarball release** — the one `uname -r` / kernelrelease shared by
  `/lib/modules/<ver>` and `/boot/vmlinuz-<ver>`.
- **Idempotent + non-empty sentinel.** Upload overwrites a partial prior write; the sentinel
  asserts `/boot/vmlinuz-<ver>` exists **and is non-empty** (a zero-byte kernel is always a
  failed upload — the opposite of the modules `modules.dep` sentinel, which allows empty).
- **Gated identically; KDUMP lane only.** A non-kdump System neither force-offs nor writes the
  overlay (unchanged). The upload lane (`initrd_ref`, no `modules_ref`) is untouched.
- **Seam split keeps it unit-tested.** Orchestration (ordering, gate, error contract) is pure
  and fake-tested; only the libguestfs `upload`/`mkdir_p`/`statns` calls are `live_vm`-gated.

## Scope

In scope:

- `GuestKernelWriter` protocol (renamed from `GuestModuleWriter`) gains a kernel-image
  parameter on `inject`.
- `LocalLibvirtInstall._inject_built_modules` passes the already-staged kernel path to the
  writer.
- `_RealGuestModuleWriter.inject` uploads the kernel to `/boot/vmlinuz-<ver>` and verifies the
  non-empty sentinel in the same rw session.
- ADR-0207 + this spec; ADR index row.

Out of scope:

- Enabling `kdump.service` in the guest image (image-level, addressed by rebuilding with
  current rootfs-builder code).
- Staging `System.map-<ver>` / `config-<ver>` (not needed by `kexec -p` or dracut; the build
  publishes neither).
- Any change to the MCP surface, ports, DB schema, build plane, or the upload (`initrd_ref`)
  lane.

## Acceptance

### CI (host-free, fakes)

- The from-source KDUMP install hands the writer the staged kernel image path, and the writer
  records it; force-off precedes fetch precedes inject (existing ordering assertions extended).
- A non-kdump System with a `modules_ref` does **not** force-off, fetch, or hand the writer a
  kernel.
- `_RealGuestModuleWriter` derives `/boot/vmlinuz-<ver>` from the modules-tarball version (the
  `_read_release` contract is reused; the kernel filename uses the same `<ver>`).
- The kernel sentinel rejects a zero-byte upload (positive-size check), distinct from the
  modules sentinel.

Because the CI acceptance verifies plumbing (the writer is handed the kernel, the sentinel
rejects an empty upload) but **cannot** verify that the resulting overlay actually arms kdump,
the bare-path dependency named in Root cause is confirmable only on hardware. A green CI run is
not evidence that kdump arms.

### Live (hardware, runbook / `live_vm`)

**Precondition:** the run must use a guest image rebuilt with `kdump.service` enabled (gap 2,
out of scope here) so a failure isolates to the kernel-staging change rather than the image.

A local-libvirt run that boots a from-source kdump kernel, `control.force_crash`, and
`vmcore.fetch method=kdump` returns a real redacted vmcore (`/var/crash/<ts>/vmcore` harvested
from the overlay), with `introspect.from_vmcore` yielding a non-empty `tasks` report. This
verification is hardware-gated (it needs a KVM host) and is a runbook exercise, consistent with
ADR-0203/0206.

## References

- [#666](https://github.com/randomparity/kdive/issues/666), [#654](https://github.com/randomparity/kdive/issues/654)
- [ADR-0207](../../adr/0207-local-libvirt-stage-kernel-into-guest-boot.md),
  [ADR-0206](../../adr/0206-modules-in-guest-shared-contract.md),
  [ADR-0203](../../adr/0203-local-libvirt-kdump-overlay-harvest.md),
  [ADR-0030](../../adr/0030-install-boot-plane.md)
- `src/kdive/providers/local_libvirt/lifecycle/install.py`
  (`LocalLibvirtInstall.install`, `_inject_built_modules`, `_RealGuestModuleWriter`)
