# ADR 0363 — A fadump guest-RAM floor enforced at admission

- **Status:** Accepted
- **Date:** 2026-07-15
- **Composes with:** [ADR-0349](0349-ppc64le-fadump-opt-in.md) (fadump opt-in; the
  `debug.fadump` flag + the ppc64le/reservation preconditions this floor sits beside),
  [ADR-0355](0355-power-native-kvm-hv-validation.md) (recorded the native-POWER fadump
  readiness failure as a follow-up, not a regression), [ADR-0341](0341-tcg-deadline-scaling.md)
  (the accelerator-keyed boot-deadline multiplier — the *only* existing readiness accommodation,
  which this ADR deliberately does **not** extend)
- **Issue:** #1181 · Epic: #1139

## Context

ADR-0349 made fadump an opt-in capture method for ppc64le Systems: `debug.fadump=True`
plus a `crashkernel` reservation resolves the System to `CaptureMethod.FADUMP` and adds
`fadump=on` to the boot cmdline. The two preconditions the profile validator already
enforced are `arch=ppc64le` and a present reservation.

The native KVM-HV validation on real POWER (#1156, ADR-0355) drove the full spine on a
POWER9 host. Every step went green **except fadump**: the `fadump=on` kernel boots under
KVM-HV, but the run-readiness check then fails, so the crash→capture cycle never starts.
The kdump variant — same guest, same kernel, same bundle, `method kdump` — **passes** on
the same host at the same 2 GiB profile. The only difference is the `fadump=on` boot.

The cause is memory, not time. On POWER, fadump reserves a *boot-memory region* on top of
the `crashkernel` reservation (the region the production kernel is re-launched into after a
crash), and the fadump kernel re-registers that region on first boot. At the 2 GiB profile
the boot-memory reservation plus `crashkernel` leaves too little for userspace to reach the
kdive-ready marker at all. kdump reserves only the `crashkernel` region, which is why it
clears readiness at the same size.

There was no fadump-specific memory floor (the ADR-0349 validator requires only ppc64le +
a reservation, no RAM lower bound) and no fadump-specific readiness accommodation (the sole
boot-window tuning is `tcg_deadline_multiplier(accel)`, which returns `1.0` for KVM, so a
native-POWER fadump boot gets zero extra slack).

## Decision

Enforce a **fadump minimum guest-RAM floor of 4096 MiB (4 GiB)** at profile validation.

- A new module constant `FADUMP_MIN_MEMORY_MB = 4096` in `kdive.profiles.provisioning`.
- A new `ProvisioningProfile` model validator, `_require_fadump_memory_floor`, that rejects
  a fadump profile whose **concrete** `memory_mb` is below the floor with
  `CONFIGURATION_ERROR`. It sits beside the existing ADR-0349 fadump precondition validator.
- The check fires only on a concrete `memory_mb`. Sizing fields are optional at parse
  (ADR-0067/ADR-0024 delta): a shape-sized allocation omits `memory_mb`, and admission
  reconciles it to a concrete size and **re-parses** the stored profile
  (`_stored_profile_for`), so the floor is enforced on the size that actually boots. A
  shape-sized fadump allocation requesting `memory_gb=2` is therefore rejected at
  `systems.provision`/`systems.define` (pre-capacity-commit), returning a typed envelope —
  not a boot that fails readiness after consuming an allocation.
- The #1181 native-POWER proof profile (`test_live_stack.py`) is bumped from 2048 to 4096
  MiB, with its paired `allocations.request` reserving `memory_gb=4`, and the POWER host
  bring-up runbook §7 records the floor and the fadump proof as passing.

The floor is a direct root-cause fix. 4 GiB is chosen from the #1156 evidence (2 GiB fails
readiness under `fadump=on` while kdump clears it at the same size) and is the size the
native-POWER fadump proof provisions at. The proof record
`docs/design/2026-07-15-power-native-fadump-ram-floor-1181-proof-record.md` documents the target
host's fadump readiness (QEMU 10.2.1 ≥ the ADR-0349 floor, KVM-HV) and the end-to-end
crash→capture status.

## Rejected alternatives

- **A fadump-specific readiness accommodation** (extend the boot-window deadline / add a
  fadump-registration-aware readiness signal, ADR-0341 style). Rejected: the failure is a
  genuine memory shortage, not slowness — userspace never reaches ready because there is too
  little RAM after the reservation, and no amount of extra deadline recovers that. A deadline
  accommodation would turn a fast, clear rejection into a slow readiness timeout. (An
  independent boot-window need would still be handled by the existing `accel` multiplier.)
- **Enforcing the floor only in the test profile** (bump the proof to 4 GiB, no validator).
  Rejected: an operator could still provision a 2 GiB fadump System and hit the same opaque
  readiness failure. The floor belongs at admission so the profile that cannot work is
  rejected with an actionable `configuration_error`.
- **Enforcing at the libvirt renderer / boot handler** instead of the profile. Rejected: the
  fadump preconditions already live on `ProvisioningProfile` (ADR-0349), admission fails
  pre-flip so a rejection consumes no capacity, and a later-stage rejection would already
  have committed the allocation.
- **A higher floor (e.g. 8 GiB) or a RAM-scaled reservation.** Rejected as speculative: 4
  GiB is the size the live proof validated; a larger floor would reject working
  configurations without evidence.

## Consequences

- fadump Systems require ≥ 4 GiB guest RAM; a smaller request is rejected at admission with
  a clear message naming the floor. kdump and every non-fadump method are unaffected.
- The floor is a single named constant co-located with the fadump preconditions, so a future
  change to the validated size is one edit.
- No migration and no production-schema change: the validator runs at parse, and the only
  stored fadump profiles are the proof fixtures, which are updated in the same change.
