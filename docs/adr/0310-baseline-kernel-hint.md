# ADR 0310 — Optional `baseline_kernel` hint disambiguates a multi-kernel `/boot`

- **Status:** Accepted
- **Date:** 2026-07-03
- **Deciders:** kdive maintainers
- **Issue:** #1016 (`BLACK_BOX_REVIEW.md` Finding 2(b), Epic #1018)
- **Spec:** [baseline-kernel-hint-1016](../archive/superpowers/specs/2026-07-03-baseline-kernel-hint-1016.md)
- Extends ADR-0272 (provision-time baseline-kernel boot) and ADR-0295 (direct-kernel provisionable
  signal); supersedes nothing.

## Context

A local-libvirt `direct-kernel` provision boots the rootfs's own `/boot` kernel, extracted
host-side (ADR-0030/0272). `select_kernel_and_initrd` picks the baseline fail-closed: it excludes
`*-rescue-*` images and raises `configuration_error` on zero or more-than-one non-rescue
`vmlinuz-*` rather than guessing a version order. A silent wrong pick boots a dead guest that still
reports `ready` (the #905 symptom), so ADR-0272/0295 deliberately fail closed and reject guessing.

The gap (BLACK_BOX_REVIEW Finding 2(b)): there is no explicit escape hatch. A rootfs with more than
one kernel — e.g. the `fedora-kdive-ready-43` `virt-builder` debug image — is unprovisionable, and
because a failed provision consumes the Allocation (one-System-per-Allocation, ADR-0149), fixture
selection is destructive trial-and-error. ADR-0295 records `direct_kernel` as a discovery signal so
a caller *learns* a multi-kernel image is not provisionable, but offers no way to provision one.

## Decision

Accept an optional operator hint that names the intended baseline kernel, disambiguating a
multi-kernel `/boot`. Fail-closed stays the default when the hint is absent.

1. **Hint field: `LibvirtProfile.baseline_kernel`.** Direct-kernel provisioning is a local-libvirt
   concern (the profile validator pairs `disk-image` with the remote-libvirt section only), so the
   hint is a field on the `local-libvirt` provider section beside `rootfs`/`crashkernel`:
   `baseline_kernel: NonEmptyStr | None = None`. An absent field is byte-identical to today's
   profile — back-compatible. Its `Field` description states it disambiguates a multi-kernel `/boot`
   and accepts either the full `vmlinuz-<ver>` filename or the bare `<ver>`.

2. **Selection honors the hint, still fails closed.** `select_kernel_and_initrd(boot_entries,
   hint=None)`:
   - **No non-rescue candidate** → raise "no bootable kernel" **regardless of the hint**. Rescue
     images are never a baseline, so there is nothing valid to name; the hint cannot resurrect an
     un-bootable image.
   - **Hint present** → resolve it against the non-rescue candidates. A candidate `kernel` matches
     when `kernel == hint` or `kernel == "vmlinuz-" + hint` (filename or bare version). No match
     raises `configuration_error` with the sorted `candidates` and the echoed `hint`, so the
     operator can self-correct without host access.
   - **Hint absent** → unchanged: more than one candidate raises the unambiguous-selection error;
     exactly one is selected.
   The hint is validated whenever present, so a stale hint against a single-kernel image fails
   loudly rather than being silently ignored — the value is an explicit operator assertion.

3. **Thread the hint through the existing extraction seam.** `ExtractBaselineKernel` becomes
   `Callable[[Path, Path, str | None], BaselineKernel]`; `_real_extract_baseline_kernel` forwards
   the hint to `select_kernel_and_initrd`. `LocalLibvirtProvisioning.provision` reads
   `section.baseline_kernel` and passes it through `_prepare_baseline_kernel` to the extractor. The
   reuse-on-retry path (a present per-System baseline directory, ADR-0060/0272) is unchanged — the
   hint is only consulted during a fresh extraction, so an idempotent retry stays stable.

4. **The `direct_kernel` capability signal (ADR-0295) is unchanged.** It predicts the *default*
   (no-hint) selection outcome, which stays "exactly one non-rescue candidate is provisionable". The
   hint is an explicit-operator escape hatch layered on top; the discovery signal still honestly
   reports a multi-kernel image as `not_provisionable` for the no-hint path.

## Consequences

- A multi-kernel rootfs is now provisionable by naming its baseline kernel in the profile; the
  common single-kernel path is untouched (no hint needed).
- The hint is an explicit choice, not a guess: it cannot mask an un-bootable image, and a wrong hint
  surfaces the available candidates rather than booting a dead guest.
- No schema, migration, RBAC, or config change: the field is an optional addition to an existing
  pydantic/TOML profile section.

## Alternatives rejected

- **Auto-pick the newest kernel.** ADR-0272/0295 rejected guessing a version order — a silent wrong
  pick is the #905 failure mode. The hint is an explicit operator assertion, compatible with
  fail-closed.
- **Let the hint override an empty/rescue-only `/boot`.** There is no valid baseline to name; the
  image genuinely cannot direct-kernel boot. The "no bootable kernel" error stays authoritative.
- **Silently ignore a hint that names no present kernel.** A stale hint would then boot an
  unexpected kernel (or, on a single-kernel image, mask a typo). Validating the hint whenever
  present keeps the operator's assertion honest.
- **A top-level (provider-agnostic) profile field.** The concept is local-libvirt direct-kernel
  extraction; remote-libvirt `disk-image` boots the operator-staged base image's own kernel and
  never reads it.
