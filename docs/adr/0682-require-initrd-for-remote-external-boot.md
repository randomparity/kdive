# 0682 — Require an initrd for remote external boot

## Status

Accepted (2026-09-26)

Amends ADR-0583's 2026-09-25 root-argument rule for
providers without a whole-disk root device.

## Context

Stage inspection records a filesystem `UUID=` root. An initrd can resolve it;
the kernel's early block lookup cannot. Local-libvirt can substitute its owned
whole-disk device. Remote-libvirt owns no such device, so an external Run boot
without an initrd reaches activation with `root=UUID=` and cannot mount root.

## Decision

Plan admission rejects a build without an initrd when the provider supplies no
whole-disk root device. It gives the caller a configuration error instructing
them to supply an initrd with the build. Admission precedes activation creation.
Plans with an initrd retain the inspected root token; local-libvirt's whole-disk
substitution remains unchanged. The plan schema and persisted records do not change.

## Consequences

An unsupported remote boot fails at admission rather than after guest startup.
A caller must rebuild with an initrd before retrying. A future remote root
provenance implementation may supersede this rejection once it can provide an
early-kernel-resolvable root token.

## Considered & rejected

- **Keep the current plan.** verified: `external_boot_root_arguments` in
  `src/kdive/services/external_boot/plan.py` returns the inspected `UUID=` token
  without an initrd or provider device (main at `9d8dbbbab`); ADR-0583's
  2026-09-25 amendment records that early block lookup cannot resolve it.
- **Record and boot by PARTUUID.** judgment: stage inspection and provenance
  would expand this repair beyond admission; future remote-libvirt work owns it.
