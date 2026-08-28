# 0583 — Provider-aware INITRD component input

## Status

Accepted (2026-08-27)

## Context

ADR-0563 narrowed component-source declarations to inputs that reach enforcement. INITRD now needs
its first caller entry point. Local-libvirt direct-kernel boots can use a worker-host initrd, while
remote-libvirt regenerates `/boot/initramfs-<version>.img` inside the guest before adding its GRUB
slot. Replacing that guest-specific image can leave the inherited `root=UUID=...` command line
pointing at storage the supplied initrd cannot mount.

## Decision

`ProvisioningProfile` gains an optional provider-neutral `initrd` component reference. Admission
passes that reference to `reject_unsupported_component_source` as `ComponentKind.INITRD` before a
provision job is queued.

Local-libvirt accepts only the `local` source kind. On a fresh baseline materialization it copies
the allowlisted worker-host file into the atomic baseline directory as `initrd`, replacing the
initramfs extracted from the rootfs. A retry reuses that directory and therefore the same bytes.
Fault-inject accepts `local` for contract testing but performs no host materialization.

Remote-libvirt declares no accepted INITRD source kinds. Any supplied INITRD is rejected during
admission with the existing `configuration_error` details: provider, component kind, submitted
source kind, and the empty accepted-source list. Remote continues generating its initramfs in the
guest and preserving the bootloader's default root-device command line.

## Consequences

Profiles without `initrd` retain their current behavior. The optional field is persisted with the
profile and remains visible in the generated tool schema. A local supplied initrd is selected only
for the System's baseline boot; Run install artifacts keep their existing per-Run initrd path.

The declaration/enforcement parity guard now recognizes INITRD as enforced. No component upload,
catalog lookup, artifact fetch, or remote supplied-initrd path is introduced.

## Considered & rejected

- **Keep the current profile surface.** judgment: this leaves local-libvirt's supported
  direct-kernel INITRD input unreachable and does not satisfy issue #1436's requested outcome.
- **Accept INITRD on remote-libvirt.** verified: `deploy/remote-libvirt-guest-helpers/
  kdive-install-kernel` runs `dracut --force` and passes that generated image to `grubby
  --copy-default`; issue #1436 records why a worker-host image cannot preserve the guest storage
  assumptions this step supplies.
- **Expose the field but only validate it.** judgment: an accepted local input that provisioning
  never consumes would be a phantom capability.
- **Put INITRD only in the local provider section.** judgment: remote callers would receive a
  structural unknown-field error instead of the provider/component/source rejection contract the
  issue requires.
