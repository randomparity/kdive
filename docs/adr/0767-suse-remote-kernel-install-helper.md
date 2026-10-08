# 0767 — SUSE remote kernels use native dracut and GRUB installation

## Status

Proposed

## Context

Issue #3082 requires a Leap remote image and actual uploaded-kernel install/boot.
The existing RHEL remote helper is Fedora BLS/grubby-specific. The pinned local Leap
15.6 representative instead contains GRUB 2.12, dracut and an XFS root filesystem;
it has no grubby. Its persistent default is index zero. Adding a higher-version
kernel to automatic discovery could change that default.

The first canonical package build failed in repo2solv with `No space left on
device` on the pinned image's small XFS root, before helper installation. The
host had ample capacity. Package-not-found messages followed failed repository
refresh and do not establish package-name defects. Expired signing-key and
metadata warnings are retained; verification is not relaxed.

## Decision

Add one x86_64 `opensuse-leap-15.6-kdive-remote-base`
cloud-image row matching the local Leap representative. Pin its official source
URL and SHA-256 in the catalog. Pass an optional per-image `cloud_image_checksum`
to the existing get_url checksum argument, as explicitly approved by the operator;
other catalog rows retain their current behavior. This is one input to the existing downloader, not a refresh framework.

For this exact pinned Leap row only, prepare a fresh 10 GiB qcow2 before package
customization. This uses the existing native-builder size baseline, not a new
catalog sizing API or an all-distribution default. Keep the verified downloaded
source immutable and separate; preserve prior failed attempts. Refuse unfamiliar
source geometry, missing native XFS expansion capability, insufficient host space,
or an already-present partial output. Use native virt-resize to copy and expand
only the third XFS partition into the new disk, preserving boot regions, partition
starts and root identity. Verify these postconditions before promoting the fresh
output to the existing customization path. A failed transform never reaches package
installation or staging; retain its output for diagnosis and require a fresh owned
work directory for retry. No host tool installation or package-trust change is added.
Actual build/free-space and existing boot/lifecycle proofs establish sufficiency.

Select a SUSE source variant for `kdive-install-kernel` from the existing catalog
`distro=opensuse-leap`; retain the canonical installed basename, owner, permissions,
argv, four subcommands and ADR-0489 exit classification. The worker protocol and
Fedora helper remain unchanged. Reuse the merged Debian selection seam without
changing its family behavior.

Install the uploaded single-release kernel and generated dracut initramfs under
`/boot/kdive`, outside the distribution's automatic kernel scan. Replace only a
KDIVE-owned `/etc/grub.d/42_kdive` generator, with one stable `kdive` entry/ID.
Regenerate `/boot/grub2/grub.cfg` through `grub2-mkconfig`. Use its exported root
identity and native `grub-mkconfig_lib` boot-device preparation and filesystem-relative
paths. Follow the inspected native UUID/PARTUUID/device fallback; preserve ordinary
root and command-line defaults, append requested words, and remove inherited
crashkernel words when the request contains none. Encode generated arguments as
GRUB words; never evaluate caller text as shell code. Preserve other generators,
persistent/default/saved selection and distribution kernels. `install` does not
select a boot. `boot` calls `grub2-reboot kdive` before detached reboot.

For the existing kdump method, set only the active `KDUMP_KERNELVER` assignment in
`/etc/sysconfig/kdump` to the installed absolute kernel path and enable
`kdump.service`; do not start it against the old running kernel. Native mkdumprd
supports that absolute path and maintains its own `/var/lib/kdump/initrd` and
kernel link. Preserve unrelated kdump settings; non-kdump installation leaves them
alone. Positive crash-loaded status is required whenever kdump arming is claimed.
No crash-capture success is inferred from service enablement or boot.

## Consequences

The two SUSE remote baseline cells become executable through the existing carrier.
The existing family-aware installed-kernel digest observer selects the exact
SUSE `/boot/kdive` path without fallback for both current consumers. Existing Fedora/RHEL
paths remain unchanged. Existing images require rebuilding from the candidate;
helper source hashes and final image/package identity accompany live evidence.

The inspected image uses Wicked. The explicitly approved Leap-only native policy
and return routing are recorded in ADR-0768 and are required image prerequisites.
Provider forwarding follows the existing ADR-0751 deployment. Neither a manually
patched guest nor a passing unit test substitutes for either required live cell.
Native POWER and changes to the Debian helper remain outside this change.

## Considered & rejected

- Keep the Fedora helper — **verified:** virt-inspector on the SHA-pinned Leap15.6
  Build19.146 source lists native GRUB2/dracut and no grubby; current helper invokes grubby.
- Install grubby or replace the guest network/initramfs stack — **judgment:** unnecessary family
  policy changes; use the distribution's existing tools.
- Put the uploaded kernel in the automatically scanned directory — **verified:**
  virt-cat of the pinned image shows GRUB_DEFAULT=0 and native 10_linux scans
  /boot/vmlinuz-* in version order; an extra release can change index zero.
- Change the provider protocol or add a bootloader framework — **judgment:** no new capability
  requires either surface.

Primary references:

- [Leap GRUB guide](https://doc.opensuse.org/documentation/leap/reference/html/book-reference/cha-grub2.html).
- [Leap kdump configuration](https://manpages.opensuse.org/Leap-15.6/kdump/kdump.5.en.html).
- [Official image directory](https://download.opensuse.org/distribution/leap/15.6/appliances/).
- Existing contracts: ADR-0082 and ADR-0489; local Leap catalog source pin.
