# ADR-0355: POWER native KVM-HV validation + ppc64le operational bring-up

Status: Accepted

Issue: #1156 · Epic: #1139 (full ppc64le support) · Supersedes: none · Depends on: ADR-0339
(admission arch-validate + persist accel), ADR-0349 (ppc64le fadump opt-in), ADR-0352 (per-arch
guest-accel diagnostics), ADR-0353 (`live_vm_tcg` tier), ADR-0354 (host-arch/guest symmetry
invariant — "live POWER proof deferred to #1156")

## Context

The ppc64le design (`2026-07-13-ppc64le-full-support.md`, decision 3) proved the full spine only
under TCG on the x86_64 host and gated the POWER-native proof on hardware. ADR-0354 recorded the
same gap: the host-arch/guest-symmetry invariant was audited and unit-tested, but "live POWER proof
deferred to #1156." With a POWER9 host available, this ADR records the native KVM-HV validation and
the operational facts it surfaced — the parts of "POWER support" that only appear on real hardware,
where the code is already portable but the *deployment* is not.

The validation host is a POWER9; the KVM-HV path is architecturally identical on POWER10, so this
decision and the runbook are POWER-generic. Evidence:
`docs/design/2026-07-15-power-native-kvm-hv-validation-1156-proof-record.md`.

## Decision

### 1. The spine proofs assert the host-resolved accelerator, not a constant

Admission persists the accelerator on the System row from libvirt capabilities (ADR-0339): `kvm` for
a native guest arch, `tcg` for a foreign one. The #1144 proofs asserted the persisted value against a
hard-coded `"tcg"` — true only because the validation host was x86_64. On a POWER host the same
ppc64le guest is native, so `accel=kvm`, and the ssh-reachability proof failed `assert 'kvm' ==
'tcg'`.

A test-only `expected_accel(arch)` helper (live_stack conftest) resolves the accel the host actually
produces — native arch + usable `/dev/kvm` → `kvm`, else `tcg` — mirroring the production
`guest_arch_accel` probe (ADR-0352). The proofs assert against it. They keep their names and the
`live_vm_tcg`+`live_stack` markers, so the ADR-0353 tier and its guard are unchanged; the same proof
now exercises TCG on x86_64 and KVM-HV on POWER.

### 2. `check-local-libvirt.sh` probes the ppc64le host kernel name

libguestfs builds its supermin appliance from the host kernel (ADR-0222). On ppc64le the kernel is
`/boot/vmlinux-*` (ELF, no `z`), not `vmlinuz-*`; the readable-kernel probe globbed only `vmlinuz-*`
and passed vacuously on POWER, letting the "kernel unreadable" fault through to build time. The probe
now checks both patterns.

### 3. The mock-OIDC issuer runs as native JVM bytecode, not an emulated image

The live-stack backends pull on ppc64le **except** `mock-oauth2-server` and `grafana`, which publish
no ppc64le manifest. Emulating the issuer container under qemu-user deadlocks (the JVM segfaults).
The operational answer, documented in the runbook: run the issuer's portable jib bytecode
(`/app/classes` + `/app/libs/*.jar`, architecture-independent) on a **native ppc64le JDK**. Grafana
(observability only) has no ppc64le image and is run elsewhere or skipped.

### 4. drgn requires libkdumpfile on ppc64le

drgn has no ppc64le wheel and builds from source. Without `libkdumpfile-dev` present at build time it
reads ELF cores but not kdump-compressed vmcores, so kdump *capture* fails
(`drgn was built without libkdumpfile support`) even though boot/ssh proofs pass. The runbook lists
`libkdumpfile-dev` (with autotools + elfutils) as a required pre-build dependency.

## Consequences

- The native KVM-HV spine (provision→boot→crash→kdump→retrieve) is proven on POWER and repeatable via
  the runbook; the `live_vm_tcg` proofs cover the POWER (KVM-HV) rows without new tests or a tier
  change.
- Native fadump is the one spine step not green: the `fadump=on` kernel boots under KVM (past the
  #1151 TCG RTAS Oops) but readiness fails at the profile's 2 GiB guest RAM. Folded forward as a
  follow-up (a larger guest-memory profile or a fadump-specific readiness accommodation), not a
  regression — kdump, the spine, is green.
- The ppc64le backend-image gap is an operator concern, not a code change: kdive's Python builds from
  source on ppc64le, but two pinned upstream container images do not exist for ppc64le. The runbook
  carries the native-JDK issuer recipe and the grafana caveat.

## Rejected alternatives

- **Add parallel native `live_vm` proofs.** Doubles the proof set and forces reworking the ADR-0353
  disjointness guard; the accel-aware assertion covers the KVM-HV rows with the existing proofs.
- **Emulate the OIDC container via qemu-user binfmt.** The JVM deadlocks/segfaults under emulation on
  this host; the native-JDK path runs the same bytecode reliably.
- **Rebuild/publish a ppc64le mock-oauth2-server image.** Heavier and redundant — the jib layers are
  already arch-neutral; a native JRE is all that is missing.
- **Rename the proofs / retire "under_tcg".** Would churn the ADR-0353 tier guard's pinned names for
  no behavioral gain; the accel-aware assertion plus updated docstrings suffice.
- **Block on native fadump.** Would gate a proven native-KVM-HV spine on a memory-profile follow-up;
  documented as a verdict instead (mirrors the ADR-0349/#1151 precedent).
