# Headless lane-image console policy

Issue #3236; campaign 2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18.
Scope token q3236-75c339bb; operator scope and concrete design approved 2026-10-16.

## Problem

The image role sets a serial GRUB terminal only for Rocky/x86_64. Fedora/x86_64
uses the same image-role boot path and needs the same policy before staging.
The Fedora POWER catalog images use a separate direct-kernel path, so adding a
GRUB rewrite there would target a bootloader they do not run.

## Scope and ownership

Extend the existing guest_base_image task to Fedora and Rocky on x86_64. Keep
its current script, pre-stage placement, effective force calculation, and staged
image skip. The role remains the owner; no caller migration, new entry point,
dependency, CLI, image family or architecture is needed. ADR0778 is reserved but
unused: this applies existing policy without changing an architectural decision.

Fedora POWER producers remain unchanged. `rootfs_build.py` repacks catalog
inputs to whole-disk ext4 qcow2 for direct kernel boot (ADR-0272).
`arch_traits.py` selects `hvc0`; GRUB terminal customization is not applicable
to those resulting images. This source-grounded reason is not a current boot
qualification. The full native POWER matrix remains with #2818.

## Behavior and validation

The affected x86_64 task removes existing terminal/input/output and serial
command assignments, appends one serial/console terminal and 115200-baud serial
command, then runs `grub2-mkconfig -o /boot/grub2/grub.cfg`. A failing command
fails the role before staging. No fallback or suppressed error is added.

On Linux, the existing console harness will execute that actual extracted shell script
against a temporary GRUB defaults file, with only the two guest absolute paths
mapped into the temporary directory. A harmless `grub2-mkconfig` executable on
PATH checks its arguments and captures the rewritten defaults. The harness
checks conflicting/absent assignments, unrelated content preservation, repeated
execution, regeneration ordering, and propagation of regeneration failure.
Other developer hosts retain the guard/order checks and explicitly report the Linux
script proof as not run; Linux CI executes that required proof without a new host tool.
Its Jinja guard matrix covers the catalog family names, x86_64/ppc64le/aarch64,
staged/absent images and both global/per-image force. This is script and guard
evidence; it cannot prove an Ansible image build or a real guest boot.

## Success

1. Fedora and Rocky x86_64 select the existing serial task before staging when
   absent or forced; staged unforced images skip it. Other named family/arch
   combinations do not select this task.
2. Extracted-script tests prove the rewrite, idempotence and fail-fast behavior.
3. Operator docs record the Fedora POWER GRUB reason and distinguish local
   harness success from actual rebuilt-image qualification.
4. After separate exact-host approval, one Fedora x86_64 image is rebuilt with
   this candidate role. Record source/package/GRUB and output identity; prove
   headless serial boot, authenticated OS/architecture, reboot, owned-resource
   cleanup and host restoration. Missing live proof leaves this issue parked.

## Failure model

1. **Actors and deployments**
   - Operators running the existing image play on native x86_64 libvirt hosts.
   - Linux CI/developers running the harmless extracted-script proof; other supported
     developer hosts run guard/order checks and explicitly report script proof not run.
   - Existing Fedora POWER build-fs/direct-kernel consumers, unchanged.
2. **Invariants and assets at stake**
   - A graphical-only GRUB must not strand the affected headless x86_64 images.
   - Failed customization must stop before staging; staged unforced bases survive.
   - Tests cannot certify live boot or mutate the host GRUB configuration.
3. **Accepted failure classes**
   - Package mirrors and virt-builder inputs can fail; existing commands fail the
     build, and no new retries or promotion behavior is introduced.
   - Non-listed new families/architectures are outside this approved change.
4. **Covered elsewhere**
   - Native POWER release qualification: #2818.
   - Existing image admission and helper/security policy: role and current gates.
   - Exclusive lab reservation and restore authority: campaign root/operator.
