# Authority local-libvirt TCG readiness window

## Problem

The authority lane captures a 900-second console readiness window regardless of the
guest accelerator. Its provision intent expires after 15 minutes, so scaling only the
console window would create an inconsistent configured budget. Issue #2799 requires the
authority lane to use ADR-0341's accelerator multiplier and reject an invalid configured
window-to-intent relationship.

## Scope

At local authority composition, resolve `tcg_deadline_multiplier(manifest.accel)` once.
Multiply the 15-minute intent lifetime and the configured console window by the same
factor. Reject a configured base window longer than the base intent lifetime before
constructing the provider. Pass the multiplier into the authority readiness seam;
the external-boot caller retains the default factor of one. KVM retains its current
15-minute lifetime and base console window. This is a nominal configuration
relationship: the intent clock starts before domain work, so it does not guarantee
a full console window remains after that work.

The manifest currently accepts only `kvm`, which makes a TCG authority configuration
unreachable. The operator approved adding `tcg` on 2026-09-26. Retain `kvm` as the
default, align the Ansible deployment validator, and keep unknown accelerator values
invalid. Domain XML already accepts TCG.

The regular provision path and external-boot deadlines belong to their existing
owners. The multiplier default remains governed by ADR-0341.
The authority provider's default intent lifetime remains the single source for
the composition check and scaled value.
[ADR-0683](../../adr/0683-scale-authority-readiness-with-tcg.md) records the
nominal budget decision.

### Failure model

Invalid window-to-intent configuration fails during composition with an actionable
`ValueError`, before provider construction or guest mutation. Existing readiness
failure behavior remains unchanged. A TCG manifest without the emulator required
by domain XML continues to fail at that existing boundary.

## Success

KVM's authority window and intent lifetime remain at their base values. A TCG
authority manifest uses the configured multiplier for both. An oversized configured
window fails composition. The external-boot caller continues using the base window.

## Validation

- `focused-test`: KVM and TCG composition yields matching scaled window and intent
  lifetime; an oversized configured window raises before provider creation.
- `focused-test`: the prepared console deadline uses the supplied multiplier;
  the default preserves the external-boot behavior.
- `focused-test`: both manifest validators accept `tcg` and reject unknown
  accelerators, while retaining the KVM default.
- `task-test-not-applicable`: a live TCG authority guest requires an operator supplied
  authority host and base image; no such fixture is provided in this checkout.
