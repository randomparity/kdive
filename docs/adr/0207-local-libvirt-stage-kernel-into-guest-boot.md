# ADR 0207 — Local-libvirt install stages the from-source kernel into the guest `/boot` for in-guest kdump

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** kdive maintainers
- **Issue:** [#666](https://github.com/randomparity/kdive/issues/666)
- **Refines (does not supersede):** [ADR-0206](0206-modules-in-guest-shared-contract.md) §4
  (local install injects `/lib/modules/<ver>` into the overlay; the guest's `kdumpctl` builds
  the crash initramfs), [ADR-0030](0030-install-boot-plane.md) (the direct-kernel install/boot
  plane).
- **Builds on:** [ADR-0203](0203-local-libvirt-kdump-overlay-harvest.md) (the host-side
  read/write overlay seam and the same-host fact this reuses).
- **Spec:** [`../superpowers/specs/2026-06-21-local-kdump-stage-kernel-boot-666.md`](../archive/superpowers/specs/2026-06-21-local-kdump-stage-kernel-boot-666.md)

## Context

ADR-0206 closed most of the local-libvirt from-source kdump gap: the build publishes a
`modules_ref`, and `LocalLibvirtInstall.install` injects `/lib/modules/<ver>` into the
per-System qcow2 overlay so the guest's `kdumpctl` can build a crash initramfs against the
custom kernel's module tree. A live end-to-end run on a real KVM host — driven through the MCP
tool surface — confirmed the injection chain works: after `runs.install`, the overlay's
`/lib/modules/7.0.0` is present.

But `control.force_crash` still produced **no vmcore**, and `vmcore.fetch method=kdump` failed
with `readiness_failure`. The kdump capture environment never armed.

The root cause is a second gap ADR-0206 did not cover. Local-libvirt uses
`boot_method: direct-kernel`: the running kernel is supplied to the domain by libvirt as the
`<kernel>` element (a host-staged `bzImage`), and is **never written into the guest `/boot`**.
A standard in-guest kdump arms by kexec-loading a crash kernel image from
`/boot/vmlinuz-$(uname -r)`. Under direct-kernel boot, `uname -r` reports the from-source
version (e.g. `7.0.0`), but the guest `/boot` contains only the base image's distro kernels
(`vmlinuz-6.17…`, `vmlinuz-6.18…`) — there is no `vmlinuz-7.0.0`. So `kdumpctl` has no crash
kernel to load even with `/lib/modules/7.0.0` present and `kdump.service` enabled. Module
injection alone cannot arm in-guest kdump under direct-kernel boot.

(A separate, image-level gap — Fedora ships `kdump.service` disabled, and the image used in the
live run predated the rootfs builder's `systemctl enable kdump.service` — is addressed by
rebuilding the guest image with current code and is out of this ADR's scope. This ADR is the
code gap: the kernel image is missing from the guest `/boot`.)

The kernel bytes are already on the worker at install time: `install()` fetches `kernel_ref`
to the per-Run staging path `{staging}/{system_id}/{run_id}/kernel` for the direct-kernel
`<kernel>` element. The fix needs no new artifact and no new fetch — only to land that same
image inside the guest overlay.

## Decision

`LocalLibvirtInstall.install`, on the from-source KDUMP lane (method is `KDUMP` and a
`modules_ref` is present), stages the from-source kernel image into the guest overlay at
**`/boot/vmlinuz-<ver>`** in the **same read-write libguestfs session** that injects the
modules, where `<ver>` is the version already recovered from the modules tarball (the
`uname -r` / `make modules_install` kernelrelease — one source of truth for both the `depmod`
target and the kernel filename).

### 1. One seam, one rw mount, both artifacts

The injection seam (ADR-0206's `GuestModuleWriter`, renamed to `GuestKernelWriter` to reflect
that it now writes the kernel image too) takes the kernel image path alongside the modules
tarball. Its single rw libguestfs mount: clobbers + extracts `/lib/modules/<ver>`, runs
`depmod`, then uploads the kernel image to `/boot/vmlinuz-<ver>`. One mount, one force-off, one
quiescent-disk window — the kernel is co-located with the modules it must match, so a partial
state can never pair a new module tree with a stale kernel (or vice versa).

### 2. The kernel filename is the modules version, not a second source

`<ver>` is read from the modules tarball (`_read_release`, unchanged). Under direct-kernel
boot the running kernel **is** the from-source kernel, so `uname -r` == that kernelrelease ==
the `/lib/modules/<ver>` directory name == the `/boot/vmlinuz-<ver>` suffix `kdumpctl`
resolves. Deriving the filename from any other place (e.g. parsing the bzImage) would add a
second version source that could disagree with the modules tree `depmod` indexed.

### 3. Idempotent, with a non-empty sentinel

A failed install records no `run_steps` row (ADR-0030 §2), so a retry re-runs the whole
injection. The kernel upload is idempotent — libguestfs `upload` truncates/creates, so a
retry overwrites a partial prior write — and is verified by a sentinel: `/boot/vmlinuz-<ver>`
exists **and is non-empty**. Unlike the modules `modules.dep` sentinel (which must allow a
valid empty file for an all-builtin kernel), a zero-byte kernel image is always a failed
upload, so the kernel sentinel asserts a positive size. `/boot` is created with `mkdir -p`
first (idempotent; the Fedora rootfs already has `/boot`, but a fresh fs kind may not).

### 4. Gated identically to module injection; only the KDUMP lane

Kernel staging fires under exactly the same condition as module injection — `method is KDUMP`
and `modules_ref is not None`. A non-kdump (console/gdbstub) System direct-kernel-boots and
needs no `/boot/vmlinuz-<ver>` (its kernel comes from the libvirt `<kernel>` element), so it
neither force-offs nor writes the overlay, unchanged. The upload lane (`initrd_ref`, no
`modules_ref`) stages a domain `<initrd>` and arms kdump from that initrd; it needs no kernel
in `/boot` and is untouched.

### 5. Seam split keeps it unit-testable

The orchestration — force-off → fetch modules → inject (modules + kernel) ordering, the KDUMP
gate, and the install/boot error contract — stays pure and is unit-tested with a fake writer
that records the kernel image it was handed. Only the libguestfs `upload`/`mkdir_p`/`statns`
calls join the existing `# pragma: no cover - live_vm` real writer. Real
panic→arm→capture→harvest fidelity remains a `live_vm`/runbook exercise (it needs a KVM host),
as ADR-0203/0206 already established for this path.

## Consequences

- A local System built kdump-capable now has both `/lib/modules/<ver>` **and**
  `/boot/vmlinuz-<ver>` in the guest after install, so `kdump.service`/`kdumpctl` can build the
  crash initramfs and kexec-load the crash kernel; `control.force_crash` writes a real
  `/var/crash/<ts>/vmcore` the ADR-0203 harvest fetches. The #666 keystone gap closes.
- The install-time overlay write grows by one kernel image (tens of MB) on the KDUMP lane —
  the same image already fetched for the `<kernel>` element, written once more into the guest.
  No new artifact, no new fetch, no schema/migration change.
- The `GuestModuleWriter` seam is renamed to `GuestKernelWriter` and its `inject` method gains
  a kernel-image parameter. This is a provider-internal rename (one protocol, its real and
  fake implementations, and their imports); no MCP surface, port, or DB shape changes.
- `System.map`/`config-<ver>` are **not** staged: dracut builds the kdump initramfs from
  `/lib/modules/<ver>` (already injected, `depmod`-indexed) and `kexec -p` loads the bzImage
  directly; neither needs `System.map` or the `.config`, and the build publishes neither as an
  artifact. Staging them would add build artifacts for no kdump-path requirement.
- Real arm/capture verification stays hardware-gated; CI covers the seam contract with fakes —
  it verifies the kernel is staged and the sentinel rejects an empty upload, **not** that the
  overlay arms kdump.
- **Named dependency (hardware-only):** this relies on the guest image's `kdumpctl` resolving
  the crash kernel by the bare path `/boot/vmlinuz-$(uname -r)` (the #666 live-run mechanism),
  not via `grubby`/BLS (no entry exists under direct-kernel boot). If a live run shows otherwise,
  the contingency — pin `KDUMP_KERNELVER` in `/etc/sysconfig/kdump` and/or write a BLS entry — is
  a named follow-up, out of scope here; CI cannot detect this case.

## Considered & rejected

- **Stage the kernel in a second, separate rw libguestfs mount (its own seam/method).**
  Rejected: a second force-off + rw mount of the same overlay doubles the corruption-hazard
  window ADR-0203 force-offs to avoid and can leave a new kernel paired with a stale module
  tree if the second mount fails. One mount writes both, atomically with respect to the
  force-off window.
- **Fold the kernel image into the `modules_ref` tarball at build time and extract it to
  `/boot`.** Rejected: the modules tarball is the `lib/modules/<ver>` tree (the
  remote-consistent layout `_read_release` parses); conflating the boot kernel into it would
  change the build packaging contract and the byte layout both providers share, and the kernel
  is already fetched at install for the `<kernel>` element — no second copy needs to travel in
  the tarball.
- **Also stage `System.map-<ver>` and `config-<ver>` into `/boot`.** Rejected: neither is
  required by `kexec -p` (loads the bzImage) or by dracut's kdump initramfs build (works from
  the injected, `depmod`-indexed `/lib/modules/<ver>`), and the build publishes neither, so
  staging them means new build artifacts for no in-guest kdump need. Left for a follow-up if a
  concrete requirement appears.
- **Switch the kdump path to a boot method that installs the kernel into the guest `/boot`
  (grub/BLS install), like remote.** Rejected: that abandons local's direct-kernel boot (the
  whole-disk-ext4, no-grub model the local provider is built on) for the kdump case only,
  forking the boot path; injecting the one file `kdumpctl` needs into `/boot` keeps a single
  boot model.
- **Derive the kernel filename by parsing the bzImage version string.** Rejected: it adds a
  second version source that could disagree with the `/lib/modules/<ver>` tree `depmod`
  indexed; the modules-tarball version is the single authoritative key for both.
- **Verify the kernel upload by file existence only (mirror the modules sentinel).** Rejected:
  a kernel image is never legitimately empty, so a positive-size sentinel catches a truncated
  upload the existence-only check would false-pass — the opposite of the modules sentinel,
  whose empty `modules.dep` is valid for an all-builtin kernel.
