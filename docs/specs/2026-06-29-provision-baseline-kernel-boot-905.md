# Spec: provision-time baseline-kernel boot for local-libvirt direct-kernel (#905)

- Issue: #905
- ADR: [ADR-0272](../adr/0272-provision-baseline-kernel-boot.md)
- Status: Draft

## Problem

A local-libvirt `systems.provision` with `boot_method: direct-kernel` defines a domain that never
boots the guest OS. The System reaches `ready` (the libvirt domain is defined and started), but the
guest halts at firmware ("no bootable device") and never reaches userspace, so it is never SSH- or
drgn-reachable.

Root cause is a seam gap between two planes:

- The local-libvirt **rootfs build** (`providers/local_libvirt/rootfs_build.py`) repacks the root
  tree into a **no-partition-table, bootloader-less whole-disk ext4 qcow2** — by design, the only
  layout the direct-kernel boot path mounts (`root=/dev/vda`, ADR-0030). The image has
  `/boot/vmlinuz-<ver>` and `/boot/initramfs-<ver>.img` but no MBR/GRUB.
- The **provision** renderer (`providers/local_libvirt/lifecycle/xml.py:render_domain_xml`) emits
  `<os><type>hvm</type></os>` with no `<kernel>`/`<initrd>`/`<cmdline>` and no `<boot>`, so libvirt
  defaults to `<boot dev='hd'>`. A bootloader-less disk + a disk-boot domain = SeaBIOS finds nothing
  to boot.

A direct-kernel `<os>` is rendered **only** by the install lane
(`providers/local_libvirt/lifecycle/install.py`), which stages a kernel from a *build Run* artifact.
So a freshly-provisioned local-libvirt System is unusable for SSH/drgn-live until it goes through a
build → install — and nothing signals this; the System just reports `ready` and times out on connect.

Empirically, booting the same rootfs standalone via direct-kernel (`-kernel`/`-initrd`/`-append
"console=ttyS0 root=/dev/vda rw"`) reaches `multi-user.target`, starts sshd, and DHCPs. The image
boots fine via direct-kernel; it just cannot disk-boot, which is all provision renders.

## Goal

A local-libvirt `direct-kernel` provision boots the rootfs's **own baseline kernel**, so a
freshly-provisioned System reaches userspace and is SSH/drgn-reachable without a build → install —
the behavior the bare-fs rootfs layout, the `kernel_source_ref` docstring, and the walkthrough all
already describe. This is Option 1 in the issue.

## Non-goals

- Gating the `ready` state on a confirmed boot (Option 2's residual). Provision today returns `ready`
  after define+start without polling readiness; that contract is unchanged. The install lane keeps
  ownership of the readiness poll. This spec makes provision *able* to boot the baseline kernel; it
  does not change what `ready` asserts.
- Any change to remote-libvirt (`disk-image`, boots an operator-staged image — no direct kernel) or
  fault-inject (owns no domain XML).
- Any change to the install/boot lane's build-artifact direct-kernel path.

## Requirements

R1. For a local-libvirt `direct-kernel` provision, the rendered domain has a direct-kernel `<os>`:
`<kernel>` pointing at the rootfs's own baseline kernel, an optional `<initrd>`, and a `<cmdline>` of
`root=/dev/vda console=ttyS0 rw` (plus `crashkernel=<token>` when `local_libvirt.crashkernel` is set,
so a kdump-provisioned System reserves the crash region on the baseline boot).

R2. The baseline kernel (+ initramfs when present) is extracted **read-only** from the materialized
rootfs **base** image (not the live overlay) to a stable per-System host path that persists for the
System's lifetime (libvirt re-reads `<kernel>`/`<initrd>` on every domain `create`).

R3. `render_domain_xml` is fail-closed: a local-libvirt domain cannot be rendered without a
`<kernel>`. A missing kernel path is a `CONFIGURATION_ERROR`, not a silently disk-booting domain.

R4. An image with no `/boot/vmlinuz-*` fails provision with a `CONFIGURATION_ERROR` naming the image
as un-bootable via direct-kernel — fail fast, do not start a domain that cannot boot.

R5. Idempotent provision/retry: the baseline kernel/initrd are extracted only when absent (mirroring
the overlay's create-only-when-absent contract, ADR-0060) and written temp-then-rename so a present
file is always complete. A retry after a partial provision reuses them and never re-mounts the base.

R6. `teardown` (and therefore `reprovision`, which is teardown+provision) reclaims the per-System
baseline kernel/initrd files alongside the overlay, so a torn-down System leaves no orphaned files.

R7. The existing gdbstub (`-gdb`) and SSH-forward (`-netdev`/`virtio-net`) passthroughs still render
and compose with the new direct-kernel `<os>` — they are orthogonal `<qemu:commandline>`/`<devices>`
additions.

R8. No schema, migration, RBAC, tool-surface, or config-setting change.

## Approach

### Extraction seam (new)

`providers/local_libvirt/lifecycle/baseline_kernel.py`:

- `BaselineKernel(kernel: Path, initrd: Path | None)` — the extraction result.
- `select_kernel_and_initrd(boot_entries: list[str]) -> tuple[str, str | None]` — a **pure**,
  unit-tested helper: from a `/boot` listing, pick the newest `vmlinuz-<ver>` (numeric-aware version
  compare; the common image has exactly one) and its matching initramfs
  (`initramfs-<ver>.img` for rhel/fedora, `initrd.img-<ver>` for debian), or `None` if absent.
  Raises a `CONFIGURATION_ERROR` when there is no `vmlinuz-*` (R4).
- `ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]` — the injected seam:
  `(base_image, dest_dir) -> BaselineKernel`.
- `_real_extract_baseline_kernel` — the production seam (libguestfs read-only mount of the base,
  `glob_expand("/boot/*")` → `select_kernel_and_initrd` → `download` each to `dest_dir`, temp-then-
  rename). `live_vm`/no-cover, mirroring `_RealGuestKernelWriter`.

### Renderer (`lifecycle/xml.py`)

`render_domain_xml` gains `kernel_path: Path | None = None` and `initrd_path: Path | None = None`.
A local-libvirt domain is always direct-kernel (the profile validator pairs `disk-image` with
remote-libvirt only), so the renderer always emits a direct-kernel `<os>`:

- `<kernel>` = `kernel_path` (required; `None` → `CONFIGURATION_ERROR`, R3),
- `<initrd>` = `initrd_path` when set,
- `<cmdline>` = baseline cmdline (R1), built from a module constant + `section.crashkernel`.

The `<kernel>`/`<initrd>`/`<cmdline>` are built with `ElementTree` (no string interpolation), so a
profile/path value cannot inject XML — the same property the adversarial XML suite already asserts.

### Provisioning plane (`lifecycle/provisioning.py`)

- New injected seam `extract_baseline_kernel: ExtractBaselineKernel` (defaults to the real impl;
  `from_env` wires it).
- In `provision()`: after `materialize_rootfs` resolves `base`, extract the baseline kernel/initrd to
  the per-System path (only when absent, R5), then pass the paths to `render_domain_xml`.
- Per-System baseline paths and their removal live in `lifecycle/storage.py` next to the overlay
  helpers; `teardown`'s file cleanup removes them (R6).

### Staging location

Baseline kernel/initrd live in `ROOTFS_DIR` next to the System's overlay
(`{system_id}-baseline-kernel`, `{system_id}-baseline-initrd`) — the directory libvirt already reads
the overlay from, so teardown is symmetric (one place to reclaim) and the files outlive any single
Run (unlike the install lane's per-Run `INSTALL_STAGING`, which has a `run_id` provision lacks).

## Acceptance criteria

- A direct-kernel provision renders `<os>` with `<kernel>`, `<initrd>` (when the image has one), and
  `<cmdline>` = `root=/dev/vda console=ttyS0 rw` (+ `crashkernel=` when set). (R1)
- The kernel/initrd are extracted from the base, not the overlay, and persist after provision. (R2)
- `render_domain_xml` with `kernel_path=None` raises `CONFIGURATION_ERROR`. (R3)
- An image with no `/boot/vmlinuz-*` fails provision `CONFIGURATION_ERROR`. (R4)
- A second `provision` of the same System reuses the baseline files (extract seam not re-invoked when
  present) and does not recreate the overlay. (R5)
- `teardown` removes the overlay **and** the baseline kernel/initrd. (R6)
- gdbstub + SSH-forward passthroughs still render alongside the direct-kernel `<os>`. (R7)
- `select_kernel_and_initrd` picks the newest kernel + matching initramfs across fedora/rhel/debian
  naming, and raises on an empty `/boot`. (R4)
- Live (`live_vm`, operator-run): a fresh direct-kernel provision boots to `multi-user.target` and is
  SSH-reachable with no intervening build/install.

## Risks

- **Wrong kernel/initrd selected** → guest still halts, still reports `ready`. Mitigated by the pure
  selection helper's tests + the fail-fast on an empty `/boot`; full confirmation is the `live_vm`
  proof. Readiness-gating `ready` would catch a mis-selection but is out of scope (non-goal).
- **libguestfs read of the base adds seconds to provision.** Acceptable: provision is a worker job
  already offloaded via `asyncio.to_thread` and already runs `qemu-img`; the read is read-only and
  skipped on retry (R5).
- **An image legitimately ships a kernel with an embedded initramfs** (no separate initrd). Handled:
  `<initrd>` is omitted when none is found, mirroring the install lane.
