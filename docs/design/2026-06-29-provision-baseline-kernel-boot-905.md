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
exactly `root=/dev/vda console=ttyS0 rw` — the verified working repro (Problem section). `crashkernel`
is deliberately **not** added to the baseline cmdline: the baseline boot exists for SSH/drgn
reachability, and kdump (its `crashkernel` prerequisite, sized against the kernel-under-test) is owned
by the install/boot lane. A speculative `crashkernel` on a *different* baseline kernel could fail the
reservation or boot with reduced RAM, and provision does not poll readiness to catch it.

R2. The baseline kernel (+ initramfs when present) is extracted **read-only** from the materialized
rootfs **base** image (not the live overlay) to a stable per-System host path that persists for the
System's lifetime (libvirt re-reads `<kernel>`/`<initrd>` on every domain `create`).

R3. `render_domain_xml` is fail-closed: a local-libvirt domain cannot be rendered without a
`<kernel>`. A missing kernel path is a `CONFIGURATION_ERROR`, not a silently disk-booting domain.

R4. An image with no `/boot/vmlinuz-*` fails provision with a `CONFIGURATION_ERROR` naming the image
as un-bootable via direct-kernel — fail fast, do not start a domain that cannot boot.

R5. Idempotent provision/retry: the baseline kernel/initrd are extracted only when absent (mirroring
the overlay's create-only-when-absent contract, ADR-0060) and the skip-when-present check is **atomic
over the pair**, not per file. The kernel and its initramfs are a unit: a crash between writing one
and the other must not leave a kernel-only state that a retry skips, rendering an initrd-less `<os>`
that a modular kernel cannot boot from (the #905 symptom). The extractor stages both into a per-System
temp directory and renames it into place only after both are written (an os.rename of the directory is
atomic), so the destination is either fully present or fully absent. The absence check is the
destination directory; a retry after a partial extraction re-mounts the base and re-completes the pair.

R5a. The baseline kernel and initramfs share a per-System extraction directory
`ROOTFS_DIR/{system_id}-baseline/` holding `kernel` and (when present) `initrd`; presence of the
directory is the all-or-nothing extraction marker (R5).

R5b. Provision-before-install ordering invariant. `provision` runs once on the `provisioning -> ready`
transition; the install/boot lane then redefines the domain's `<kernel>`/`<initrd>`/`<cmdline>` to the
per-Run build kernel (`install.py`). Because `provision` redefines the domain unconditionally, it must
never run *after* an install on a live System — doing so would overwrite the install-staged build
kernel with the baseline (today it already overwrites it with a no-kernel `<os>`; the fix changes the
clobbered result, not the clobber). The only intended re-provision is `reprovision`, which first tears
the install state down (R6). The implementation confirms no worker/reconciler path re-issues the
provision job for a System past `ready`; if one is ever added it must not redefine over a staged build
kernel.

R6. `teardown` (and therefore `reprovision`, which is teardown+provision) reclaims the per-System
baseline directory (R5a) alongside the overlay, so a torn-down System leaves no orphaned files.

R7. The existing gdbstub (`-gdb`) and SSH-forward (`-netdev`/`virtio-net`) passthroughs still render
and compose with the new direct-kernel `<os>` — they are orthogonal `<qemu:commandline>`/`<devices>`
additions.

R8. No schema, migration, RBAC, tool-surface, or config-setting change.

## Approach

### Extraction seam (new)

`providers/local_libvirt/lifecycle/baseline_kernel.py`:

- `BaselineKernel(kernel: Path, initrd: Path | None)` — the extraction result.
- `select_kernel_and_initrd(boot_entries: list[str]) -> tuple[str, str | None]` — a **pure**,
  unit-tested helper. From a `/boot` listing it selects the System kernel and its matching initramfs.
  Selection rules (the load-bearing logic — a wrong pick boots a dead guest that still reports
  `ready`, so it fails closed rather than guesses):
  - Consider only `vmlinuz-<ver>` entries; **exclude `*-rescue-*`** (rhel/fedora ship a
    `vmlinuz-0-rescue-<hash>` + `initramfs-0-rescue-<hash>.img` pair that must never be selected).
  - No remaining candidate → `CONFIGURATION_ERROR` "image has no bootable kernel" (R4).
  - Exactly one candidate → select it.
  - More than one candidate → `CONFIGURATION_ERROR` naming the candidates. The kdive-ready build emits
    exactly one kernel; refusing to guess among several is safer than a fragile kernel-version
    comparison whose mistake is silent and reproduces #905. (A deterministic newest-wins rule can be
    added later behind a live proof if multi-kernel images become real.)
  - Initramfs for the selected `<ver>`: `initramfs-<ver>.img` (rhel/fedora) or `initrd.img-<ver>`
    (debian); `None` when neither is present (embedded-initramfs kernel — `<initrd>` omitted).
- `ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]` — the injected seam:
  `(base_image, dest_dir) -> BaselineKernel`.
- `_real_extract_baseline_kernel` — the production seam (libguestfs read-only mount of the base,
  `glob_expand("/boot/*")` → `select_kernel_and_initrd` → `download` each into a temp dir that is then
  atomically renamed to `dest_dir`, so the destination is all-or-nothing, R5). `live_vm`/no-cover,
  mirroring `_RealGuestKernelWriter`. When the `guestfs` Python binding is absent it raises
  `MISSING_DEPENDENCY` with an actionable message, exactly as `_RealGuestKernelWriter._mount_rw` does.
  **New host prerequisite:** this puts libguestfs on the provision path — a catalog-only provision
  previously needed only `qemu-img` + libvirt. In a local-libvirt deployment the host that provisions
  also builds/installs (which already require libguestfs), so this adds no new host; the dependency is
  named here so an operator running a provision-only worker installs the binding.

### Renderer (`lifecycle/xml.py`)

`render_domain_xml` gains `kernel_path: Path | None = None` and `initrd_path: Path | None = None`.
A local-libvirt domain is always direct-kernel (the profile validator pairs `disk-image` with
remote-libvirt only), so the renderer always emits a direct-kernel `<os>`:

- `<kernel>` = `kernel_path` (required; `None` → `CONFIGURATION_ERROR`, R3),
- `<initrd>` = `initrd_path` when set,
- `<cmdline>` = the fixed baseline cmdline module constant `root=/dev/vda console=ttyS0 rw` (R1).

The `<kernel>`/`<initrd>`/`<cmdline>` are built with `ElementTree` (no string interpolation), so a
profile/path value cannot inject XML — the same property the adversarial XML suite already asserts.

### Provisioning plane (`lifecycle/provisioning.py`)

- New injected seam `extract_baseline_kernel: ExtractBaselineKernel` (defaults to the real impl;
  `from_env` wires it).
- In `provision()`: after `materialize_rootfs` resolves `base`, extract the baseline kernel/initrd to
  the per-System baseline directory (only when the directory is absent, R5), then pass the resulting
  `kernel`/`initrd` paths to `render_domain_xml`.
- Per-System baseline-directory path and its removal live in `lifecycle/storage.py` next to the
  overlay helpers; `teardown`'s file cleanup removes the directory (R6).

### Staging location

Baseline kernel/initrd live in a per-System directory `ROOTFS_DIR/{system_id}-baseline/` (`kernel`,
optional `initrd`) next to the System's overlay — the directory libvirt already reads the overlay
from, so teardown is symmetric (one tree to reclaim, `shutil.rmtree`-style) and the files outlive any
single Run (unlike the install lane's per-Run `INSTALL_STAGING`, which has a `run_id` provision lacks).
The directory grouping also makes the extraction atomic (R5): the temp dir is renamed into place whole.

## Acceptance criteria

- A direct-kernel provision renders `<os>` with `<kernel>`, `<initrd>` (when the image has one), and
  `<cmdline>` = exactly `root=/dev/vda console=ttyS0 rw` (no `crashkernel`). (R1)
- The kernel/initrd are extracted from the base, not the overlay, and persist after provision. (R2)
- `render_domain_xml` with `kernel_path=None` raises `CONFIGURATION_ERROR`. (R3)
- An image with no `/boot/vmlinuz-*` fails provision `CONFIGURATION_ERROR`. (R4)
- A second `provision` of the same System reuses the baseline files (extract seam not re-invoked when
  the baseline dir is present) and does not recreate the overlay. (R5)
- A retry after a partial extraction (baseline dir absent because the temp dir was never renamed in)
  re-mounts the base and re-completes the pair; a present baseline dir is never half-populated. (R5)
- `teardown` removes the overlay **and** the per-System baseline directory. (R6)
- gdbstub + SSH-forward passthroughs still render alongside the direct-kernel `<os>`. (R7)
- `select_kernel_and_initrd`: selects the lone kernel + matching initramfs across fedora/rhel
  (`initramfs-<ver>.img`) and debian (`initrd.img-<ver>`) naming; **excludes** a `*-rescue-*` pair;
  raises `CONFIGURATION_ERROR` on an empty `/boot`, on no non-rescue kernel, and on >1 non-rescue
  kernel; returns `initrd=None` for an embedded-initramfs kernel. (R4)
- Live (`live_vm`, operator-run): a fresh direct-kernel provision boots to `multi-user.target` and is
  SSH-reachable with no intervening build/install.

## Risks

- **Wrong kernel/initrd selected** → guest still halts, still reports `ready` (the #905 symptom).
  Mitigated structurally: `select_kernel_and_initrd` excludes rescue images and **fails closed** on
  any ambiguity (zero or >1 non-rescue kernel) rather than guessing a version order, and pairs the
  initramfs to the selected kernel's exact version. The pure helper is adversarially unit-tested
  (rescue pair, debian naming, multi-kernel, empty `/boot`, embedded-initramfs). Full confirmation is
  the `live_vm` proof; readiness-gating `ready` would also catch a mis-selection but is out of scope
  (non-goal).
- **libguestfs read of the base adds seconds to provision.** Acceptable: provision is a worker job
  already offloaded via `asyncio.to_thread` and already runs `qemu-img`; the read is read-only and
  skipped on retry (R5).
- **An image legitimately ships a kernel with an embedded initramfs** (no separate initrd). Handled:
  `<initrd>` is omitted when none is found, mirroring the install lane.
