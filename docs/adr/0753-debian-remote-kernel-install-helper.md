# 0753 — Debian-family remote kernel installation uses native initramfs and GRUB tools

## Status

Accepted (2026-10-07)

## Context

Issue #3081 requires the Ubuntu remote image to install and boot uploaded kernels.
The image role currently installs the Fedora helper, which calls `dracut`, `grubby`
and `grub2-reboot`. The Debian-family image supplies different tools.
[ADR-0082](0082-remote-install-in-guest-kernel.md) defines one installed helper and
one deterministic boot slot; [ADR-0489](0489-guest-helper-exit-code-names-transience.md)
defines its exit codes. Neither requires identical family internals.

## Decision

Keep the existing helper command, installed path and exit contract. Select a
Debian variant from the existing catalog `distro` value (`debian` or `ubuntu`);
other image families retain their current helper. Do not add an inventory input.
The variant uses `update-initramfs`, `update-grub`, and `grub-reboot`. The image
catalog installs those tools and their direct prerequisites.

Stage the uploaded kernel and its generated initramfs below `/boot/kdive`, outside
Debian's automatic `/boot/vmlinuz-*` discovery. Add or replace one dedicated
`/etc/grub.d/42_kdive` generator producing the `kdive` menu entry and ID.
The generator uses the distribution GRUB environment and `grub-mkconfig_lib` for
boot-device access and filesystem-relative paths. Retain the distribution root
selection and ordinary kernel options; append requested options, dropping inherited
`crashkernel` options when the request contains none. Encode arguments as GRUB
words, never executable shell fragments. Preserve the operator's other generators,
GRUB defaults and saved selection. `install` never selects the next boot; `boot`
uses `grub-reboot kdive` and the existing detached-reboot contract.

Use `update-initramfs -b /boot/kdive` in create or update mode for the requested
release. For the existing `kdump` method, replace only the active `USE_KDUMP`,
`KDUMP_KERNEL` and `KDUMP_INITRD` assignments in `/etc/default/kdump-tools` with
`1` and the actual isolated kernel/initramfs paths; preserve unrelated settings.
Enable `kdump-tools.service` without starting it against the old running kernel.
After boot, the native service loads those explicit artifacts; status still reads
the kernel crash-size and crash-loaded files. Non-kdump install leaves this
configuration alone. The native service can emit its ordinary-layout discovery
diagnostic before honoring explicit paths; loaded status, not that diagnostic or
unit enablement, establishes arming. Do not manufacture a kernel
configuration, change the uploaded-bundle format, or silently replace the image's
initramfs infrastructure. Ubuntu 24.04's native tool permits an absent build
configuration with a warning; the real boot proof must establish usable output.

## Consequences

The existing remote proof observers select the kernel location from their known
image family, with no fallback to an unrelated file. Register the existing Ubuntu
catalog row as the Debian representative and remove only the resolved #3081
family blocker; SUSE and observed external prerequisites retain their owners.

Images must be rebuilt from the matching checkout. Existing images and Fedora
helpers retain their behavior. Repeated installs replace the KDIVE generator and
entry without changing the distribution's persistent default. A failed install
can leave kernel/module files, as in the existing contract, but reports failure
and does not reboot. One-shot boot readiness remains a new boot identity; tests
also inspect the actual running release to reject an accidental fallback boot.

Native POWER, SUSE, guest SSH return routes and authority DWARF remain separate
obligations. This change proves the existing Ubuntu cloud-image path, not every
possible Debian filesystem, bootloader or capture configuration.

## Considered & rejected

- **Use the Fedora helper unchanged.** verified: at `e0c9b7f8`, a Debian-shaped
  tool PATH reaches `dracut: command not found` and exits 1 after bundle extraction.
- **Install an ordinary `/boot/vmlinuz-<release>` and regenerate GRUB.** verified:
  GNU GRUB's [simple configuration](https://www.gnu.org/software/grub/manual/grub/html_node/Simple-configuration.html)
  selects the highest kernel first; that can change an index-based persistent default.
- **Change the worker's allowlist or add family-specific commands.** judgment:
  the image owns family packaging; duplicating the provider protocol adds no capability.
- **Replace initramfs-tools with dracut.** judgment: unnecessary image policy change
  when the supplied Ubuntu tools support the requested install flow.
