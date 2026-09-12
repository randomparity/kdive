# 0646 — Provision the ppc64le guest emulator through libvirt_stack

## Status

Accepted (2026-09-12)

- **Issue:** #2402

## Context

`live_vm_tcg` exercises ppc64le guests. The host role previously installed only the emulator
matching the host architecture, leaving its advertised TCG tier unavailable after provisioning.
Debian-family and SUSE-family repositories package the ppc64le emulator; Enterprise Linux does
not package a foreign-architecture emulator.

## Decision

`libvirt_stack` installs the native emulator and ppc64le emulator on Debian-family and
SUSE-family hosts. RedHat-family hosts remain native-only. The family-to-guest availability map
is role-local because it selects packages for privileged provisioning; ADR-0641 keeps diagnostic
package advice separate.

## Consequences

Provisioned Debian-family and SUSE-family x86_64 hosts can run the ppc64le TCG tier. RedHat-family
hosts clearly do not promise that tier, while retaining their native `qemu-kvm` installation.

## Considered & rejected

- **Install a foreign emulator on every family.** verified: the platform-support package table and
  ADR-0641 establish that Enterprise Linux has no foreign-architecture emulator package.
- **Reuse the setup-dependency resolver.** verified: ADR-0641 records that diagnostics and the
  provisioning role answer different package-selection questions.
