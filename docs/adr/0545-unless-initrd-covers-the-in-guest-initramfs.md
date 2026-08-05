# 0545 — `UNLESS_INITRD` asks whether a module can load, not whether an initrd was uploaded

## Status

Accepted (2026-08-05)

## Context

[ADR-0544](0544-kernel-config-clause-model.md) §2 gave a clause a three-valued built-in
requirement, and defined `UNLESS_INITRD` as "`=y` unless the build uploaded an initrd artifact".
Its consequences section is explicit that this is a statement about the *artifact* and not about
the boot: `result.initrd_ref is not None` means "this build uploaded an initrd artifact", **not**
"this kernel can load a module before root is mounted". It accepted one known false positive on
those terms — a kernel with an embedded `CONFIG_INITRAMFS_SOURCE` initramfs has no `initrd_ref`
and boots fine, and kdive cannot see inside the kernel image.

That reading holds on the direct-kernel lane, which is the only lane 0544 had in view. It does not
hold on the disk-image lane, and there the cost is not one incidental false positive but a
deterministic one on every Run:

- `runs.complete_build` supplies exactly one fact —
  `rootfs_mount_warning(conn, uid, has_initrd=result.initrd_ref is not None)` — with no term for
  how the target boots.
- The disk-image lane never uploads an initrd and always builds its own. The in-guest installer
  (`deploy/remote-libvirt-guest-helpers/kdive-install-kernel`) runs `depmod`, then
  `dracut --force /boot/initramfs-$ver.img "$ver"`, then points `grubby` at it; the guest boots
  through its own bootloader off that initramfs.
- The escape the payload offered was a no-op there. An `initrd` upload is accepted on any Run —
  `RUN_ARTIFACT_NAMES` is not gated by target kind — so an agent *could* silence the advisory by
  uploading one, but the remote-libvirt install plane never reads it: `composition.py` accepts no
  `initrd` component ("initrd is future work in the parity epic (#1423)") and nothing else under
  `providers/remote_libvirt/` references one. The only way out was to upload a file that changes
  nothing about the boot, purely to quiet a check.

So every disk-image Run whose config carries a modular `EXT4_FS`/`XFS_FS`/`VIRTIO_BLK` drew
`kernel_missing_boot_config` on a kernel that boots, with no way to silence it that means anything.
That is
the shape a distro `.config` ships, and `docs/operating/external-build-upload.md` steers an agent
towards exactly such a config ("start from the catalog image's own config, not a bare
`defconfig`"). The registry already stated the exemption in prose and no seam applied it: the
`VIRTIO_PCI` comment in `requirements.py` reads "(a guest that boots through its own bootloader
and builds an initramfs with dracut is not affected either way)".

The signal is advisory — the completion succeeds and the Run still reaches `succeeded` — so this
is a wrong signal rather than a broken lane. But an advisory that fires on every Run of a whole
target kind, on the config the docs recommend, is one an agent learns to ignore, which costs the
cases where it is right.

[#1860](https://github.com/randomparity/kdive/issues/1860) implemented 0544's recorded semantics
deliberately rather than extending them unilaterally. Records here are immutable, so widening the
condition is a new decision, not an edit to 0544's decided prose.

## Decision

### 1. `UNLESS_INITRD` states a boot-ordering condition, and two independent facts answer it

The enum value asks whether *anything* can load a module before root is mounted. Two facts answer
it, and **either one alone** relieves the clause of needing `=y`:

| fact | keyword | supplied from |
|---|---|---|
| the build uploaded an initrd artifact | `has_initrd` | `result.initrd_ref is not None`, off the finalized `BuildStepResult` |
| the target boots through its own bootloader and builds its initramfs in-guest | `guest_builds_initramfs` | `run.target_kind is ResourceKind.REMOTE_LIBVIRT` |

This restates rather than replaces 0544 §2's shape: the clause states a condition and the seam
supplies the answer. Both keywords default to `False` — the strict, over-warning reading — so a
seam that forgets either over-warns rather than falling silent, which is the direction
[ADR-0330](0330-complete-build-missing-boot-config-warning.md) chose for this warning and 0544 §2
chose for `has_initrd`. `support.unmet_clauses` and `support.unmet_advertised_clauses` take both,
as does `rootfs_mount_warning`.

### 2. The boot-model fact keys on the Run's resource kind, not on its provisioning profile

`disk-image` is a `BootMethod` on the provisioning profile (`profiles/provisioning.py`), while a
Run carries a `ResourceKind` (`local-libvirt` / `remote-libvirt` / `fault-inject`). The seam keys
on the resource kind, for three reasons:

- **They are biconditional, and enforced as such.** `_pair_boot_method_with_provider`
  ([ADR-0080](0080-remote-provisioning-disk-image-profile.md)) rejects any profile that sets
  `disk-image` without a remote-libvirt provider section or vice versa, and
  `providers/local_libvirt/lifecycle/xml.py` restates the other half ("a local-libvirt domain is
  always direct-kernel"). `fault-inject` is direct-kernel too, so the test is remote-libvirt
  specifically and not "not local-libvirt".
- **It costs no read.** `_complete_authorized_build` already loaded the `Run` to authorize the
  call, so the fact reaches `_success_envelope` as a parameter — the same argument 0544 §2 makes
  for `result.initrd_ref`, and the same reason: a second read would make the advisory depend on a
  row being visible on this connection at this moment.
- **It survives the decoupled path.** A Run's `system_id` is `None` until `runs.bind`
  ([ADR-0169](0169-decouple-build-system-binding.md)), so resolving the profile is not always
  possible at completion time; `target_kind` is always present.

### 3. The embedded-initramfs false positive stays accepted, narrowed to the direct-kernel lane

`CONFIG_INITRAMFS_SOURCE` remains invisible to kdive, and neither fact above detects it. 0544's
acceptance of that false positive stands unchanged — it just stops applying to a whole target
kind, and the summary and served doc are corrected to say which lane it survives on.

## Consequences

- **The advisory becomes lane-dependent, and a reader has to know that.** The same `.config`
  produces `missing_boot_config` on `local-libvirt` and silence on `remote-libvirt`. That is the
  point, but it means the payload alone no longer tells an agent whether its kernel is modular —
  `built_in_required` on the direct-kernel lane is the only place that shows. An agent that
  develops against a disk-image target and later switches to direct-kernel gets the warning for
  the first time on a config it had been completing cleanly. Building the symbols in is correct on
  both lanes and the docs still say so.
- **The relief is about timing, not capability.** A disk-image guest whose kernel carries no
  virtio-blk driver *at all* still cannot mount its root — dracut has nothing to package — and
  still warns. Only the `=m`-versus-`=y` distinction moves.
- **The trust is in the in-guest installer, not in a check.** Nothing at completion time proves
  dracut will succeed for the uploaded kernel; the relief is a statement about the lane's design.
  A dracut failure surfaces at install (`kdive-install-kernel` calls `die`), which is where it can
  be seen, rather than as a config advisory that would have been right for the wrong reason.
- **Invariant I2 gains a third axis.** `tests/kernel_config/test_requirements.py` pins that a
  clause carries a conditional requirement only where every seam evaluating its feature supplies
  the condition. `UNLESS_INITRD` now has two conditions, so `SeamFacts` carries three fields and
  the boot-model half gets its own check. `crash_capture` answers `False` on the new axis for the
  same reason it answers `False` on the initrd one — its refusal seams hold neither a
  `BuildStepResult` nor a `Run` — which keeps it correctly untaggable.
- **The interim paragraph #1860 added to `docs/operating/external-build-upload.md` is removed**,
  and the served twin regenerated with `just resources-docs`. It documented the gap honestly while
  it existed; leaving it would be a doc that fails when followed.
- **`#1423` gets no easier and no harder.** If remote-libvirt ever accepts an `initrd` component,
  `has_initrd` starts answering `True` on that lane too and the two facts simply agree. Nothing
  here has to be unwound for that.

## Considered & rejected

- **Drop `UNLESS_INITRD` from the `rootfs_mount` clauses and go back to accepting `=m`.** Silences
  the false positive by silencing the true one as well — a modular `VIRTIO_BLK` on a direct-kernel
  boot really does panic the guest, which is the whole of #1860. It trades a wrong warning for a
  silent completion followed by an unbootable kernel, the exact failure ADR-0330's warning exists
  to prevent.
- **Resolve the Run's provisioning profile and read `boot_method` directly.** The most literal
  reading of "how does this target boot", and the one the issue's framing suggested. It needs a
  System that may not be bound yet (ADR-0169), so it either adds a read that can fail or a branch
  that falls back to the strict default on precisely the decoupled Runs the disk-image lane uses.
  The validator already makes the two equivalent, so the extra read buys a rephrasing of a fact
  already in hand.
- **Fold the boot model into `has_initrd` at the seam** — pass `has_initrd=uploaded or remote`.
  A one-line change with no signature churn, but it makes the parameter's name false and moves the
  decision out of the layer that documents it. The support checks would then hold a fact called
  "the build uploaded an initrd" that is sometimes true when no initrd exists anywhere, and I2's
  hand-maintained seam map could never distinguish the two axes.
- **Add a fourth `BuiltIn` value for "required only on a direct-kernel boot".** Puts the lane on
  the clause instead of on the seam. But the lane is a fact about the Run, not about the symbol —
  `EXT4_FS` wants the same thing in both cases — and 0544's model deliberately keeps per-Run facts
  as seam-supplied keywords. It would also leave `UNLESS_INITRD` meaning "uploaded artifact only",
  so the two values would differ by which relief they honour rather than by what they require.
- **Emit the advisory on the disk-image lane with a softer reason code.** Keeps the signal while
  admitting it is not actionable. It is not a weaker claim about the kernel; it is a claim that is
  simply false for that lane, and a second reason code for "we checked and it is fine" is noise an
  agent has to learn to discard — the same cost as the false positive, plus a payload variant.
- **Detect an embedded initramfs and close that false positive in the same change.** Would make
  the condition mean what it says on every lane. kdive holds a `.config`, not a kernel image, and
  `CONFIG_INITRAMFS_SOURCE`'s value is a build-host path whose contents kdive never sees — so this
  is not deferred work with a known shape, and 0544's acceptance of it stands.
