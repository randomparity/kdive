# 0478 — Re-home the RHEL-family kdump `CONFIG_*` set on the feature registry, advisory not gated

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** [#1626](https://github.com/randomparity/kdive/issues/1626)
- **Builds on:** [ADR-0318](0318-debug-feature-config-gate.md) (feature → `CONFIG_*` registry)
- **Amends:** [ADR-0213](0213-local-kdump-in-guest-prerequisites.md) §1 and
  [ADR-0183](0183-provider-aware-platform-root-cmdline.md) §2 — the symbols each added to the
  ADR-0096 kdump build-config fragment now live on the ADR-0318 registry instead of the deleted
  fragment. Records what [ADR-0316](0316-remove-server-build-lane.md) silently dropped when it
  removed that fragment.

## Context

kdive once shipped a packaged `kdump` kernel-config *fragment*
([ADR-0096](0096-kdump-config-fragment-build-input.md)) that the server-build lane merged into the
`.config` before compiling. Two later ADRs hardened it against live failures:

- **ADR-0213 §1** added `CONFIG_SQUASHFS`, `CONFIG_SQUASHFS_ZSTD`, `CONFIG_BLK_DEV_LOOP`,
  `CONFIG_OVERLAY_FS` and `CONFIG_KEXEC_FILE` — dracut's kdump initramfs and the
  `kexec_file_load` syscall `kexec -s -p` uses.
- **ADR-0183 §2** added `CONFIG_XFS_FS` (and `CONFIG_XFS_POSIX_ACL`) — the remote base image roots
  on XFS V5 while `x86_64_defconfig` builds EXT4 and not XFS.

**ADR-0316 deleted the server-build lane, the fragment machinery, and all `.config` validation.**
The symbols went with the fragment. Only `KEXEC_FILE` survived, because ADR-0318's replacement
registry happened to list it under `crash_capture`. Nothing recorded the loss and no test could
notice it: the fragment's tests were deleted alongside the fragment, and the registry that replaced
it was written from the local-libvirt boot path, which is ext4 with no initramfs.

The regression surfaced during the #1610 live proof on a Rocky 10 guest over `qemu+tls://`. The
kernel had to be rebuilt **five times**; each missing symbol was invisible until the previous one
was fixed, because every one of them fails *after* the crash, when the guest and its evidence are
gone. `/sys/kernel/kexec_crash_size` was non-zero and the boot looked healthy throughout.

The issue filed against this names the build-config fragment as the thing to fix. That target no
longer exists. The live surface is the ADR-0318 registry in `kdive/kernel_config/requirements.py`,
served as `resource://kdive/contracts/external-build`, so the fix is retargeted there.

One claim in the issue is wrong and worth recording: `CONFIG_KEXEC_FILE` was never absent. It is
advertised under `crash_capture` and appears in `gate_required` as the OR-group
`{KEXEC, KEXEC_FILE}`. Because it is an OR, a `KEXEC`-only kernel passes `crash_capture_refusal`
while RHEL kdump — which loads via `kexec_file_load` — still captures nothing. Real gap, different
site.

## Decision

### 1. A new advertise-only `crash_capture_rhel_guest` feature carries the lost set

Add one `FeatureRequirement` with `gate_required=()` holding `XFS_FS`, `SQUASHFS`,
`SQUASHFS_ZSTD`, `EROFS_FS`, `OVERLAY_FS`, `BLK_DEV_LOOP` and `KEXEC_FILE`. `EROFS_FS` is new
relative to ADR-0213: RHEL 10's dracut builds a squash-**erofs** initramfs where RHEL 9 built a
zstd-squashfs one. Which compressor dracut picks varies by release, so the guidance asks for all of
them rather than an OR-group — an OR would let a kernel carrying only `SQUASHFS` look satisfied on
a guest whose dracut chose erofs.

`crash_capture`'s own summary now states that its symbols get the capture kernel *loaded*, not the
vmcore *written*, and names `crash_capture_rhel_guest` as the RHEL-family remainder. The
`crash_capture_refusal` remediation does the same, so the one message an agent sees when the gate
fires cannot imply the gated set is sufficient.

**Why a separate feature rather than a conditional clause.** The issue asks that the set be
described as filesystem- and initramfs-dependent rather than implied universal. The registry is a
flat feature → clause map with no guest-family axis, and cannot express "required iff the guest is
RHEL-family" *within* a feature. A separate feature row is that axis, at the granularity the
registry already has: an agent targeting a RHEL guest reads the row and builds the set; an agent
targeting anything else skips it. No schema change, no new mechanism.

**Why advertise-only.** Adding these to `gate_required` would refuse installs that work today —
every non-RHEL guest whose root and initramfs differ. kdive has no runtime signal for the guest's
OS family (`ImageEntry` carries provider/name/arch/capabilities, not a family), so a gate could not
discriminate and would have to refuse universally or not at all. Advertising is therefore the
strongest correct position, and this ADR states the limit plainly: **a kernel missing this set
still installs, still boots, still arms `crashkernel=`, and still captures nothing on a RHEL
guest.** What changed is that the agent is told so before the build instead of after the crash.

### 2. `rootfs_mount`'s filesystem requirement becomes the OR-group `{EXT4_FS, XFS_FS}`

`rootfs_mount` advertised exactly `EXT4_FS` and `VIRTIO_BLK`, and `rootfs_mount_warning` keys on
its *advertised* clauses. Advertised clauses are AND-of-OR (`support.py::_unmet`), so appending
`XFS_FS` as a second `_plain` clause would have made the `missing_boot_config` advisory fire on
**every** ext4-root local-libvirt kernel — the overwhelmingly common case. One OR-group instead:
`{EXT4_FS, XFS_FS}` for the filesystem, `{VIRTIO_BLK}` for the root device.

This makes the predicate strictly weaker, so no kernel that is silent today starts warning. It also
stops a false alarm that existed before: an XFS-root remote or agent-uploaded rootfs
(ADR-0183/0440/0441) was previously told to build in `EXT4_FS`, which its guest never mounts.

**What the warning now means.** Because kdive does not know the guest's root filesystem family, the
advisory asserts only *"this kernel can mount no root filesystem kdive boots"* — not *"this kernel
does not match your guest"*. That is the strongest claim available without a guest-family axis, and
the summary and remediation say so. A kernel with `EXT4_FS` booting an XFS guest is not caught,
before or after this change.

This refines but does not reverse #1094, which stripped a squashfs+overlay boot path from
`rootfs_mount` that did not exist anywhere in the tree. Those symbols stay out of `rootfs_mount` —
they belong to the guest's kdump initramfs, not to kdive's direct-kernel boot, and are now in
`crash_capture_rhel_guest`. Only the filesystem half moved, because XFS is a root filesystem kdive
demonstrably does boot.

### 3. The `{KEXEC, KEXEC_FILE}` gate OR-group stays as it is

Tightening it to require `KEXEC_FILE` would be a user-facing refusal change: a `KEXEC`-only kernel
captures correctly on a guest whose kdump uses the legacy `kexec_load`, and kdive cannot tell which
guest it is dealing with. Refusing universally to catch the RHEL case would break working installs
to fix a subset. `KEXEC_FILE` is instead named as a required member of `crash_capture_rhel_guest`,
where the RHEL-family framing makes its necessity conditional and legible. A regression test pins
this decision so a later "harden the gate" pass has to argue with it rather than drift into it.

### 4. Documentation

The build-lane recipe (`docs/operating/external-build-upload.md`, mirrored into the packaged
resource by `scripts/gen_doc_resources.py`) and the four-method runbook gain the RHEL-family set
with the `=y`-not-`=m` note (a crash initramfs must not depend on the primary kernel loading
modules first) and the "fails only at capture time, each omission masks the next" warning. The
`examples/local-libvirt/README.md` sample `systems.toml` had a `[[build_config]]` section that
`InventoryDoc` has not accepted since ADR-0316; it is removed and replaced with a pointer to the
contract resource.

## Consequences

- The symbols lost with the fragment are recorded and served again. An agent reading
  `resource://kdive/contracts/external-build` before building a RHEL-family capture kernel gets all
  seven at once instead of one per rebuild.
- No new refusals. `crash_capture_rhel_guest` never gates; the `rootfs_mount` change only ever
  removes warnings; the crash gate is untouched.
- The gap is narrowed, not closed. A RHEL-family kernel missing the set is advertised-against but
  not stopped, and the failure still lands after the crash for an agent that did not read the
  contract. Closing it needs a guest-family axis kdive does not have — the natural home is the
  image catalog (`ImageEntry` gaining an OS-family field, which the provision path already knows),
  at which point `rootfs_mount_warning` and the crash seams could key the conditional set on the
  Run's target System. Deliberately out of scope here.
- Verification is unit-level: the tests assert the registry, the served contract, and the advisory
  predicates. Proving a RHEL guest now captures needs a live remote run (#1610's rock10-big arm).

## Alternatives rejected

- **Add the set to `crash_capture.gate_required`.** Refuses every non-RHEL guest that legitimately
  does not need XFS or a dracut initramfs, to fix a case kdive cannot detect.
- **Append `XFS_FS` to `rootfs_mount` with `_plain`.** Fires `missing_boot_config` on every ext4
  local-libvirt kernel — trades a missed warning for a guaranteed false one.
- **A new `guest_family` field on `FeatureRequirement`.** A schema axis with exactly one consumer
  and no runtime source to compare against; premature until the image catalog can supply the value.
- **A `complete_build` advisory naming the RHEL set on every upload.** Warns on every non-RHEL
  build, which is most of them, to reach the minority that needs it.
- **Restore the build-config fragment.** ADR-0316 removed the lane that consumed it; there is
  nothing left to merge a fragment into.
