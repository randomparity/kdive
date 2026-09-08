# Spec — fadump provisioning parity (#2381)

**Issue:** #2381 · **Branch:** feat/fadump-provisioning-parity-2381 · **Date:** 2026-09-08

## Summary

Two hand-applied fixes that made the native-POWER fadump proof (#2312) pass are not in the
source. A clean reprovision loses both.

The fadump proof uses `KDIVE_GUEST_IMAGE_PPC64LE` — a `fedora-kdive-ready-44-ppc64le` image
built by `python -m kdive build-fs`, which calls `RhelFamily.customize_steps()` in
`src/kdive/images/families/rhel.py`. The `guest_base_image` Ansible role builds REMOTE LIBVIRT
images. Both paths need the fix (provisioning parity).

## Fix 1 — fadump-capture.service

`kdump.service` cannot rebuild the fadump initrd inside the kdive-supplied initrd environment.
Without a working capture path, the guest never writes `/var/crash/vmcore` and the run ends at
the 120 s timeout. The hand-fix installed a `fadump-capture.service` unit that:
- runs `Before=kdump.service` on the capture-kernel boot;
- exits 0 on normal boots via `ConditionPathExists=/proc/vmcore`;
- runs `makedumpfile -c -d 31 /proc/vmcore /var/crash/vmcore && poweroff -f`.

**build-fs path (chosen):** Add `FADUMP_CAPTURE_SERVICE_PATH` / `FADUMP_CAPTURE_SERVICE_CONTENT`
constants to `_fedora_customize.py` and inject two steps in `rhel.py` when `kexec-tools` is
present. The `ConditionPathExists` guard makes injection safe on all kdump-capable images.

**Ansible role path:** Inject conditionally via `fadump_capture: true` per-image boolean, using
`virt-customize --upload` + `--run-command`. The boolean defaults to `false` so existing images
are unchanged. The unit file lives in `roles/guest_base_image/files/`.

**Rejected: per-catalog boolean in the Python path** — requires `RootfsCatalogEntry` schema
change; `ConditionPathExists` already provides the guard without it. judgment.

## Fix 2 — bundle layout

Fedora 44 RPMs include `lib/modules/<rel>/vmlinuz` (~63 MiB). Together with `boot/vmlinuz`
(~63 MiB) this pushes the first `.ko.xz` past `_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB). The fix
is documentation: add `--exclude='lib/modules/*/vmlinuz'` to the bundle-build instructions in
`docs/operating/runbooks/live-testing.md`. No code change needed.

## Acceptance criteria

1. `RhelFamily.customize_steps()` injects `fadump-capture.service` when `kexec-tools` present.
2. `guest_base_image` role injects the unit for images with `fadump_capture: true`.
3. `just lint`, `just type`, `just lint-ansible`, `just test-ansible` pass.
4. Runbook documents `--exclude='lib/modules/*/vmlinuz'` for the ppc64le bundle.
