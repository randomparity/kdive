# ADR-0272: Provision-time baseline-kernel boot for local-libvirt direct-kernel

- Status: Accepted
- Issue: #905
- Spec: [provision-baseline-kernel-boot-905](../specs/2026-06-29-provision-baseline-kernel-boot-905.md)
- Supersedes nothing; extends ADR-0025 (provision XML), ADR-0030 (install direct-kernel `<os>`),
  ADR-0060 (idempotent provision/overlay), ADR-0218 (loopback SSH forward).

## Context

The local-libvirt rootfs build (ADR-0052/0251) repacks the root tree into a no-partition-table,
bootloader-less whole-disk ext4 qcow2 — the only layout the direct-kernel boot path mounts
(`root=/dev/vda`, no in-image bootloader, ADR-0030). But the provision-time domain renderer
(`render_domain_xml`, ADR-0025) emits an `<os>` with no `<kernel>`/`<initrd>`/`<cmdline>` and no
`<boot>`, so libvirt defaults to `<boot dev='hd'>`. A bootloader-less disk under a disk-boot domain
halts at firmware: the guest never reaches userspace and is never SSH/drgn-reachable, yet the System
reports `ready` (#905, found while live-proving #782).

A direct-kernel `<os>` is rendered only by the install lane (ADR-0030), sourcing the kernel from a
*build Run* artifact. So provision alone never boots; a build → install is required first, and nothing
signals it. The bare-fs layout, the `kernel_source_ref` docstring ("must reach 'ready' on a baseline
kernel"), and the local-libvirt walkthrough all already describe provision booting the rootfs's own
kernel — only the renderer never did.

## Decision

For a local-libvirt `direct-kernel` provision, extract the rootfs's **own** baseline kernel
(+ initramfs) from the materialized base image and render a direct-kernel `<os>`, so provision boots
the baseline kernel as documented.

1. **Extract from the base, read-only, to a stable per-System path.** A new injected seam
   `extract_baseline_kernel(base, dest_dir) -> BaselineKernel` mounts the materialized rootfs **base**
   read-only via libguestfs, selects the System's `/boot/vmlinuz-<ver>` and its matching initramfs
   (`initramfs-<ver>.img` rhel/fedora, `initrd.img-<ver>` debian), and downloads both into a temp dir
   that is atomically renamed to the per-System baseline directory `ROOTFS_DIR/{system_id}-baseline/`
   (holding `kernel` + optional `initrd`) — so the destination is all-or-nothing and a retry after a
   crash mid-extraction re-completes the pair rather than skipping a kernel-only half-state (the kernel
   and its initramfs are a unit; a modular kernel cannot boot without its initramfs). The binding being
   absent raises `missing_dependency` (mirroring `_RealGuestKernelWriter`). Selection is a pure,
   unit-tested
   helper (`select_kernel_and_initrd`) that **fails closed**: it excludes the `*-rescue-*` pair and
   raises `configuration_error` on zero or more-than-one non-rescue kernel rather than guessing a
   version order — a silent wrong pick boots a dead guest that still reports `ready` (the #905 symptom
   itself). The kdive-ready build emits exactly one kernel, so the lone-candidate path is the norm.
   Only the libguestfs read is `live_vm`/no-cover, mirroring `_RealGuestKernelWriter`. Reading the base
   (not the live overlay) avoids a rw mount of a disk QEMU may hold open, and the base is safe for
   concurrent read-only mounts.

2. **`render_domain_xml` is fail-closed.** It gains `kernel_path`/`initrd_path` params and always
   emits a direct-kernel `<os>` for a local-libvirt domain (the profile validator pairs `disk-image`
   with remote-libvirt only, so a local domain is always direct-kernel). A `None` `kernel_path` raises
   `CONFIGURATION_ERROR` — the renderer can no longer silently emit a non-booting domain. `<cmdline>`
   is exactly `root=/dev/vda console=ttyS0 rw` (the verified working repro). `crashkernel` is **not**
   added to the baseline cmdline: the baseline boot exists for SSH/drgn reach, kdump's `crashkernel`
   (sized against the kernel-under-test) is the install/boot lane's job, and a speculative reservation
   on a different baseline kernel could fail or shrink RAM with no readiness check to catch it. All
   `<os>` text is built with `ElementTree`, so no path/profile value can inject XML.

3. **Fail fast on an un-bootable image.** An image with no `/boot/vmlinuz-*` raises
   `CONFIGURATION_ERROR` naming the image — provision does not start a domain that cannot boot.

4. **Idempotent and teardown-symmetric.** The baseline kernel/initrd are extracted only when absent
   (mirroring the overlay, ADR-0060) and written temp-then-rename, so a provision retry reuses them and
   never re-mounts the base. `teardown` (hence `reprovision`) removes them alongside the overlay, so a
   torn-down System leaves no orphaned files.

Scope: local-libvirt only. No schema, migration, RBAC, tool-surface, or config-setting change. The
gdbstub and SSH-forward passthroughs (ADR-0210/0218) are orthogonal and still compose.

## Consequences

- A freshly-provisioned local-libvirt System boots its baseline kernel and is SSH/drgn-reachable with
  no intervening build → install — closing the #782 live-proof gap. The walkthrough and
  `kernel_source_ref` claims become accurate.
- Provision gains a read-only libguestfs mount of the base (seconds), skipped on retry. It runs in the
  worker's `asyncio.to_thread` provision offload, alongside the existing `qemu-img` overlay create.
- New host prerequisite: the provision path now requires the libguestfs `guestfs` binding, which a
  catalog-only provision did not previously need (only `qemu-img` + libvirt). A local-libvirt host that
  provisions also builds/installs (both already require libguestfs), so no new host is introduced; an
  operator running a provision-only worker must install the binding, and its absence is a clean
  `missing_dependency`, not a raw import error.
- A subsequent build → install still redefines the domain with the build kernel: the install lane
  removes and re-adds `<kernel>`/`<initrd>`/`<cmdline>`, so the baseline `<os>` is cleanly replaced.
- `render_domain_xml`'s contract is stricter (a kernel path is mandatory); its only production caller
  is `provision`, which always supplies one.

## Considered & rejected

- **Option 2 — docs-only / gate `ready` on a confirmed boot.** Correcting the docs leaves provision
  non-functional for SSH/drgn (the issue's actual blocker). Gating `ready` on a readiness poll is a
  larger, separable change to the provision contract (provision becomes blocking, needs the readiness
  seam) and does not, by itself, make the guest boot. Rejected as the primary fix; a mis-selection
  guard via readiness-gating is noted as possible future work, not this issue.
- **Extract from the live overlay instead of the base.** A rw mount of an overlay QEMU may hold open
  corrupts it (the very reason `_inject_built_modules` force-offs first); a read-only mount would still
  need the base reachable. Reading the base directly is simpler and safe for concurrent readers.
- **Stage to the install lane's per-Run `INSTALL_STAGING`.** Provision has no `run_id`, and the
  `<kernel>` must outlive any Run. `ROOTFS_DIR` next to the overlay keeps one teardown site.
- **Re-extract on every provision.** Wasteful and unlike the overlay's create-only-when-absent
  contract; idempotent reuse keeps a retry cheap and stable.
- **`virt-get-kernel` subprocess instead of the libguestfs binding.** The binding read-only mount
  mirrors the existing `_RealGuestKernelWriter` (same inspect/mount of the same bare-fs layout) and
  keeps the selection logic in a pure, testable Python helper rather than parsing a tool's output dir.
- **Render a `<boot dev='hd'>` disk boot by shipping a bootloader in the rootfs.** Reverses the
  deliberate bare-fs / direct-kernel layout (ADR-0030/0052) the whole local-libvirt path is built on.
