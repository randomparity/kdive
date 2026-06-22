# ADR 0212 — Local-libvirt from-source kdump: in-guest kernel-config and image prerequisites

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** [#688](https://github.com/randomparity/kdive/issues/688)
- **Refines (does not supersede):** [ADR-0207](0207-local-libvirt-stage-kernel-into-guest-boot.md)
  (corrects its named hardware-only contingency: the residual no-vmcore gap is **not** BLS /
  `KDUMP_KERNELVER` resolution), [ADR-0096](0096-kdump-config-fragment-build-input.md) (the
  packaged kdump kernel-config fragment this extends).
- **Mirrors:** [ADR-0084](0084-remote-control-two-phase-vmcore-retrieve.md) §1 (the remote base-image
  `kernel.unknown_nmi_panic=1` obligation; local needs the equivalent on its `kdive-ready` image).

## Context

ADR-0207 staged the from-source kernel into the guest `/boot/vmlinuz-<ver>` so `kdumpctl`
could resolve and kexec-load it. It then named a hardware-only contingency: if a live run still
showed no vmcore, the suspect was BLS / `KDUMP_KERNELVER` resolution under direct-kernel boot.

The #679 (B5) live kdump verification on the dev KVM host ran that next pass and falsified the
guess. `kdumpctl` resolves the from-source `7.0.0` kernel fine; the residual no-vmcore is a
**cascade of in-guest prerequisites** the from-source kernel config and the `kdive-ready` image
do not satisfy. Each gap masked the next — `kdumpctl` cannot reach the kexec step until the
crash initramfs builds, and the NMI→panic path does not matter until the crash kernel loads — so
none was visible to CI (the marked `live_vm` path needs a KVM host), and ADR-0207's CI fakes
cannot detect them. The guest journal on the harvested overlay (ADR-0203) showed, in order:

1. `dracut[E]: Module 'squash-squashfs' cannot be installed` → `mkdumprd: failed to make kdump
   initrd`. Fedora `kdumpctl` builds a zstd-compressed **squashfs** kdump initramfs; the
   from-source kernel lacks `CONFIG_SQUASHFS` / `CONFIG_SQUASHFS_ZSTD` (and the loop/overlay
   backing dracut's squash module uses). *Validated live:* adding them makes the crash
   initramfs build (`/var/lib/kdump/initramfs-7.0.0kdump.img`, ~35 MB).
2. `kdumpctl: syscall kexec_file_load not available` → `kexec: failed to load kdump kernel`.
   `kdumpctl` loads the crash kernel with `kexec -s -p` (the `kexec_file_load` syscall); the
   fragment had `KEXEC`+`KEXEC_CORE` but not `CONFIG_KEXEC_FILE`.
3. `NMI received for unknown reason` with no panic. Local `control.force_crash` injects an NMI
   (`virsh inject-nmi`); without `kernel.unknown_nmi_panic=1` the guest ignores it. The remote
   provider already documents this as a base-image obligation (ADR-0084 §1); local's
   `kdive-ready` image never set it.
4. `keyctl: command not found` in the `kdumpctl` path — the image lacks the `keyutils` package.

A fifth gap may remain (with 1–4 applied and an NMI injected, `/var/crash` was still empty on
the live run), but the guest's journald is volatile (~3 s persists) and the worker — running
unprivileged — cannot read `virtlogd`'s `root:0600` console log, so the post-arm sequence was
not captured. Pinpointing any further gap needs a readable console (worker-as-root or wired
console readability, a separate operator prerequisite). That is explicitly **not** closed here.

## Decision

We will make the local-libvirt from-source kdump path satisfy the in-guest prerequisites the
live run identified, in the two artifacts that own them, and **honestly scope out** the
unconfirmed fifth gap.

### 1. Kernel-config fragment (`build_configs/data/kdump.config`, ADR-0096)

Add the five Kconfig symbols the from-source kernel needs for `kdumpctl` to build the squashfs
crash initramfs and kexec-load the crash kernel:

- `CONFIG_SQUASHFS=y`, `CONFIG_SQUASHFS_ZSTD=y` — the zstd-squashfs kdump initramfs dracut builds.
- `CONFIG_BLK_DEV_LOOP=y`, `CONFIG_OVERLAY_FS=y` — the loop/overlay backing dracut's squash module.
- `CONFIG_KEXEC_FILE=y` — the `kexec_file_load` syscall `kexec -s -p` uses.

These live in the **packaged fragment** (the durable source of truth every from-source kdump
build inherits), not in any per-host file-authoritative `[[build_config]]`. The fragment is
`=y` (built-in), consistent with the existing kdump symbols, so the crash environment needs no
extra modules loaded.

A fragment `=y` line is a *request*, not a guarantee: `make olddefconfig` re-resolves Kconfig
and drops any symbol whose base-config dependencies are unmet. The build orchestrator already
guards this — `_validate_final_config` reads the post-`olddefconfig` `.config` and, via
`_dropped_fragment_symbols`, raises a `CONFIGURATION_ERROR` naming any dropped fragment symbol
*before* `make` runs (`providers/shared/build_host/orchestration.py`,
`providers/shared/build_host/common.py`, ADR-0096). So each new symbol relies on the base config
satisfying its Kconfig deps (`CONFIG_KEXEC_FILE` → x86_64 `ARCH_SUPPORTS_KEXEC_FILE`;
`CONFIG_SQUASHFS_ZSTD` → `CONFIG_SQUASHFS`, added alongside), and any unmet dependency surfaces as
a loud, fail-fast build error that names the dropped symbol — not a silently miscompiled kernel.
These symbols are enforced by that existing fragment-survival guard; they are not added to the
separate `REQUIRED_KERNEL_CONFIG` group list (which is for symbols required independent of the
fragment), so there is no second source to keep in sync.

### 2. The `kdive-ready` debug image (rootfs builder)

The debug image, and only the debug image (the `kdump` capability), ships:

- `kernel.unknown_nmi_panic=1` as `/etc/sysctl.d/99-kdive-kdump.conf`, so an injected NMI drives
  the panic→kdump path — the local equivalent of the remote base-image obligation (ADR-0084).
  Written by `virt-builder` under the **same `kdump-utils`-in-packages gate** that already
  enables `kdump.service`, so a non-kdump image (e.g. the build-host toolchain) never gets it.
- the `keyutils` package (provides `/bin/keyctl`, which `kdumpctl` invokes), added to the
  debug rootfs package set `DEFAULT_DEBUG_FS_PACKAGES`. Package-set membership is the existing
  mechanism for the debug image's crash toolchain (`kdump-utils`, `kexec-tools`, `makedumpfile`).

### 3. Scope is the three proven gaps plus the keyctl finding; the fifth gap stays open

This ADR closes gaps 1–4 (the three live-validated config gaps plus the `keyutils` finding). It
does **not** claim a verified end-to-end vmcore: gap 5, if real, needs a readable console to
diagnose and is a named follow-up requiring a #679 re-run with the worker able to read the
console. Real arm→panic→capture→harvest fidelity remains a `live_vm`/runbook exercise, as
ADR-0203/0206/0207 already established — CI cannot detect any of these.

## Consequences

- A from-source kdump kernel built with the packaged fragment can build its crash initramfs and
  kexec-load the crash kernel; a `kdive-ready` debug image panics on the injected NMI and has
  `keyctl` available. The first three documented arming failures no longer occur.
- The from-source kernel `.config` grows by five `=y` symbols (squashfs+zstd, loop, overlay,
  kexec_file). Slightly larger kernel; no module-load or initramfs change at boot for the
  primary kernel (the symbols matter to the kdump crash kernel/initramfs path).
- The debug image grows by the `keyutils` package and one sysctl drop-in. The build-host image
  is unchanged (the sysctl is `kdump`-gated; `keyutils` is debug-package-only).
- No MCP surface, port, schema, or migration change. The fragment edit re-publishes through the
  existing ADR-0096 seed path (new sha256 → seed upserts the updated bytes); an operator- or
  config-owned override is still skipped, unchanged.
- Verification is layered. Unit tests assert the fragment contains the five symbols and that the
  debug build stages the sysctl + `keyutils` — necessary but not sufficient (fragment text is not
  the built `.config`). The build-time olddefconfig **drop-guard** (`_dropped_fragment_symbols`)
  is the real on-build check that each symbol survives into the kernel `.config`, failing the
  build with the dropped symbol named if a base dep is unmet. Neither proves the guest *arms*
  kdump; that end-to-end proof stays hardware-gated — the honest signal is the #679 re-run.
- ADR-0207's named contingency (BLS / `KDUMP_KERNELVER`) is recorded as **not** the cause; if a
  future live run reopens kernel resolution, this ADR does not preclude it, but it is not the
  observed gap.

## Considered & rejected

- **Set `kernel.unknown_nmi_panic=1` unconditionally on every local image.** Rejected: the
  build-host toolchain image is an ephemeral kernel-build VM; an unexpected NMI panicking it
  mid-build is a regression, and it never runs `force_crash`. Gating on the same
  `kdump-utils`-in-packages condition that already enables `kdump.service` keeps NMI→panic to
  the images that want it, matching the existing capability boundary.
- **Put `unknown_nmi_panic` in the kernel-config fragment instead of the image.** Rejected: it
  is a runtime `sysctl`, not a Kconfig symbol — there is no `CONFIG_*` for it. The kernel default
  is configured at boot from `/etc/sysctl.d`, which is guest-image state. Mirroring the remote
  base-image obligation (ADR-0084) keeps the sysctl with the userspace that owns it.
- **Rely on the operator's file-authoritative `systems.toml` kdump fragment** (which #679 patched
  live with gaps 1–2 to unblock the investigation). Rejected: that is per-host and ephemeral —
  it unblocks one dev host, not every from-source kdump build. The durable fix is the packaged
  fragment (ADR-0096) every build inherits.
- **Add `keyutils` via a `virt-builder --install` in the builder rather than the package set.**
  Rejected: the debug image's crash toolchain (`kdump-utils`, `kexec-tools`, `makedumpfile`) is
  declared in `DEFAULT_DEBUG_FS_PACKAGES`; `keyutils` is the same kind of dependency and belongs
  beside them, so the package set stays the single list of what the debug guest installs.
- **Claim an end-to-end verified vmcore now.** Rejected: the live run did not produce one (gap 5
  unconfirmed), and the console needed to diagnose it was unreadable. Asserting success CI cannot
  verify would be a phantom claim; the ADR ships the proven gaps and names the open one.
- **Set the squashfs/loop/overlay symbols as `=m` (modules).** Rejected: the existing kdump
  fragment symbols are all `=y`, and a kdump crash initramfs must not depend on the primary
  kernel having loaded extra modules first; built-in keeps the crash environment self-contained.
