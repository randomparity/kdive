# Optional `baseline_kernel` hint disambiguates a multi-kernel `/boot` (#1016)

- **Issue:** #1016 (`BLACK_BOX_REVIEW.md` Finding 2(b), Epic #1018)
- **ADR:** [ADR-0310](../../adr/0310-baseline-kernel-hint.md)
- **Status:** Draft

## Problem

A local-libvirt `direct-kernel` provision boots the rootfs's own `/boot` kernel, extracted
host-side (ADR-0030/0272). `select_kernel_and_initrd`
(`providers/local_libvirt/lifecycle/baseline_kernel.py`) picks the baseline fail-closed: it
excludes rescue images and raises `configuration_error` on zero or more-than-one non-rescue
`vmlinuz-*` rather than guessing a version order. A silent wrong pick boots a dead guest that
still reports `ready` (the #905 symptom), so fail-closed is deliberate and ADR-0272/0295 rejected
guessing a version order.

The gap: there is **no explicit escape hatch**. A rootfs with more than one kernel (e.g. the
`fedora-kdive-ready-43` `virt-builder` debug image) is unprovisionable, and because a failed
provision consumes the Allocation (ADR-0149), the only recourse is destructive trial-and-error
against a different image.

## Goal / acceptance

An operator names the intended baseline kernel through the provisioning profile, so a multi-kernel
rootfs provisions deterministically. Fail-closed stays the default.

Acceptance (from the issue):

- Provision of a multi-kernel rootfs **succeeds** when `baseline_kernel` names an existing kernel
  in `/boot`.
- **Absent** hint → still fails closed with the current unambiguous-selection error.
- A hint naming **no present kernel** → clear `configuration_error` listing the available kernels.

## Design decisions

1. **The hint is an optional local-libvirt profile field: `LibvirtProfile.baseline_kernel`.**
   Direct-kernel provisioning is a local-libvirt concern (the validator pairs `disk-image` with
   remote-libvirt only), so the field lives on the `local-libvirt` provider section beside
   `rootfs`/`crashkernel`. It is `NonEmptyStr | None`, defaulting to `None` — an absent field is
   byte-identical to today's profile (back-compatible).

2. **The hint accepts either the full `vmlinuz-<ver>` filename or the bare `<ver>`.** Resolution
   matches a candidate `kernel` when `kernel == hint` or `kernel == "vmlinuz-" + hint`, so an
   operator may copy the `candidates` list value verbatim from the fail-closed error, or supply the
   kernel version alone.

3. **The hint is honored only against the non-rescue candidates, and only disambiguates — it never
   overrides fail-closed on an empty `/boot`.** `select_kernel_and_initrd(boot_entries, hint=None)`:
   - No non-rescue kernel → raise "no bootable kernel" **regardless of the hint** (there is nothing
     valid to name; rescue images are never a baseline).
   - Hint present → resolve it against the candidates; a hint matching no candidate raises
     `configuration_error` listing the sorted candidates (`hint` echoed in details).
   - Hint absent → unchanged: >1 candidate raises the unambiguous-selection error, exactly one is
     selected.
   The hint is validated whenever present, so a wrong hint against a single-kernel image also fails
   loudly rather than being silently ignored.

4. **Thread the hint through the existing extraction seam.** `ExtractBaselineKernel` gains the
   hint parameter (`Callable[[Path, Path, str | None], BaselineKernel]`); `provision` reads
   `section.baseline_kernel` and forwards it through `_prepare_baseline_kernel` to the extractor,
   which passes it to `select_kernel_and_initrd`. The reuse-on-retry path (a present baseline
   directory) is unchanged — the hint is only consulted during a fresh extraction.

5. **The `direct_kernel` capability signal (ADR-0295) is unchanged.** It predicts the *default*
   (no-hint) selection outcome, which remains "exactly one candidate is provisionable". The hint is
   the explicit-operator escape hatch layered on top; the discovery signal still honestly reports
   that a multi-kernel image is not provisionable *without* one.

## Out of scope

- Auto-picking the newest kernel (ADR-0272/0295 rejected guessing a version order).
- Remote-libvirt (`disk-image` boots the operator-staged base image's own kernel; it never reads a
  baseline hint).
- Changing the build-time `boot_kernel_count` operand or the `direct_kernel` signal render.
