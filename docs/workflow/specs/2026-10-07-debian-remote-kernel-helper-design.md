# Debian remote kernel helper

## Problem and scope

The Ubuntu catalog image receives a Fedora-only helper. A controlled current-source
reproduction extracts a valid bundle and fails on absent dracut. The remote worker
already invokes one allowlisted helper with fixed argv; family policy belongs in
image construction and the helper, not the worker.

Implement #3081 under [ADR-0753](../../adr/0753-debian-remote-kernel-install-helper.md).
Keep Fedora behavior, provider ports, argv, stdout and exit categorization unchanged.
The existing `distro` field chooses a source variant but the installed basename,
owner and executable mode stay the same. No new inventory or public interface.

SUSE #3082, source-route #3091, authority DWARF #3131 and native POWER #2818 are
operator-approved exclusions. No changes to secret resolution or authority ownership.

## Behavior

`install` fetches and validates the existing kernel/module bundle, installs its
single release and invokes Debian initramfs tools. Create and repeat-update modes
converge. Kernel and initramfs live in `/boot/kdive`, leaving distribution kernel
scanning and default ordering alone. The sole generated KDIVE GRUB entry uses
native GRUB boot-device preparation, filesystem-relative kernel paths and the
existing root-device/UUID configuration. The generator derives its own Linux root;
`10_linux` private variables are not inherited. Follow the native decision using
exported `GRUB_DEVICE`, UUID/PARTUUID, the two Linux UUID-disable settings, local
by-UUID/by-PARTUUID existence and `uses_abstraction(..., lvm)`: use the device when
both IDs are absent, both IDs disabled, neither ID resolves, or the device uses
LVM; otherwise use PARTUUID when UUID is absent/disabled, UUID otherwise. For Btrfs,
derive `/` relative to its filesystem with `make_system_path_relative_to_its_root`,
remove one leading slash and prepend `rootflags=subvol=...` when nonempty. Prepare
access using `GRUB_DEVICE_BOOT`, independently of root, and derive both artifact
paths relative to their boot filesystem. This covers the catalog cloud image and
the named UUID/PARTUUID, separate-boot and Btrfs cases, not a new ZFS/encryption or
multipath provisioning implementation.

The generator receives distribution command-line defaults from `update-grub` and
appends the request's arguments. Parse and quote words so semicolons, quotes,
backslashes and dollar signs cannot become GRUB or shell commands. An absent
requested crashkernel removes inherited reservations; an explicit requested one
survives. Preserve default/saved boot selection and unrelated menu generators.

`boot` selects the stable `kdive` ID for one boot and schedules detached reboot
only after selection succeeds. `boot-id` and `kdump-status` retain existing output.
For `kdump`, update only active `USE_KDUMP`, `KDUMP_KERNEL` and `KDUMP_INITRD`
assignments in `/etc/default/kdump-tools`: set enabled and actual `/boot/kdive`
kernel/initramfs paths. Retain unrelated settings and comments; publish the edited
configuration only after artifacts exist. Enable the existing service, do not start
it for the pre-reboot kernel, and propagate configuration/enable failures. After
boot, require positive crash reservation and crash-loaded status for a kdump proof.
Noble's native preliminary ordinary-path discovery can print a diagnostic before
explicit paths are honored; do not patch the distro script or fabricate config.
Non-kdump install leaves the kdump configuration unchanged. This is arming evidence,
not crash-capture proof.

Missing/malformed arguments, bundles and deterministic native-tool failures return
nonzero permanent errors. Only an actual curl fetch failure returns 75; missing or
unexecutable curl stays permanent. No failed installation triggers reboot.

## Failure model

- **Prevent:** Fedora helper on the Ubuntu image; default-boot drift; duplicated
  KDIVE entries; command injection through requested kernel arguments; false
  transient classification; success after native install/selection failure.
- **Detect:** malformed archive or release identity, missing tools, initramfs/GRUB
  errors and selected-image package gaps; fail with the operation named.
- **Accept:** operator-owned guest root and GRUB configuration are trusted; existing
  bundle extraction and partial-install semantics are unchanged; interrupted writes
  can require an operator retry. Ubuntu's absent-config warning does not establish
  compression compatibility; successful actual boot is required evidence.
- **Defer:** excluded family/routing/authority/native-POWER obligations to their
  existing owners. No new exclusion is inferred from a failed required proof.

## Existing carrier migration

Add `ubuntu-2404-kdive-remote-base` (Ubuntu 24.04, x86_64) to the existing Debian
representatives and remove only its #3081 `REMOTE_BLOCKED` entry. The shared
`guest_boot_kernel` observer takes an explicit family selection and hashes exactly
`/boot/kdive/vmlinuz-<release>` for Debian, the existing path for other families.
Both the remote deep-lifecycle carrier and remote run-tool carrier bind that known
family; a missing expected file fails rather than probing an alternate path.
Update owning observer/representative/caller regressions and the existing runbook.
The two Debian deep-lifecycle baseline cells become executable; actual #3091 SSH
failure remains failed/blocked evidence under that owner rather than a fake pass.

## Success and validation

1. The existing Ubuntu catalog renders a Debian source into the canonical installed
   helper with root ownership, executable mode and existing SELinux relabel behavior;
   Fedora selection and other helper copies remain unchanged.
2. Actual helper subprocess tests cover create/repeat installation, one entry,
   unmodified persistent default and unrelated generator, inherited/requested args,
   dangerous quoting through generated GRUB syntax, UUID/PARTUUID and disabled/absent-ID
   fallbacks, LVM selection, separate `/boot`, non-root Btrfs subvolumes, kdump from
   disabled defaults with explicit artifacts, all four commands and error categories.
   Mutating host commands are isolated at their external boundary; native parsing
   and generated generator execution are exercised rather than reimplemented.
3. Build a fresh Ubuntu image through the role, then use the actual remote provider
   to upload, install and boot a real kernel. Record matching runtime/image/helper
   provenance, old/new boot IDs and expected running release. Repeat install must
   preserve one KDIVE entry and default selection. Clean only the task's new domain.
4. Required local guardrails and remote CI pass. A blocked real proof remains an
   unmet criterion on #3081; neither unit tests nor an unrelated guest boot replace it.

## Migration and rollback

Rebuild the catalog image; do not inject the new helper into existing live guests as
qualification. The image retains the original distribution boot path as fallback.
Reverting the source and rebuilding returns to the previous separately versioned
image behavior. No database migration, provider caller change or new API is involved.
