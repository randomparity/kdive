# Spec — fadump provisioning parity (#2381)

**Issue:** #2381 · **Branch:** feat/fadump-provisioning-parity-2381 · **Date:** 2026-09-08

## Summary

Two hand-applied fixes that made the native-POWER fadump proof (#2312) pass are not in the
source. A clean reprovision loses both.

The fadump proof uses `KDIVE_GUEST_IMAGE_PPC64LE` — a `fedora-kdive-ready-44-ppc64le` image
built by `python -m kdive build-fs`, which calls `RhelFamily.customize_steps()` in
`src/kdive/images/families/rhel.py`. The `guest_base_image` Ansible role builds REMOTE LIBVIRT
images. Both paths need the fix (provisioning parity).

## Fix 1 — fadump-capture.service

`kdump.service` cannot rebuild the fadump initrd inside the kdive-supplied initrd environment.
Without a working capture path the guest writes no core and the run ends at the 120 s timeout.
The hand-fix installed a `fadump-capture.service` unit that:
- runs `Before=kdump.service` on the capture-kernel boot;
- exits 0 on normal boots via `ConditionPathExists=/proc/vmcore`;
- runs `makedumpfile -c -d 31` and powers off.

**Output path is a contract, not a free choice.** The offline harvest globs
`/var/crash/*/vmcore` and `/var/crash/*/vmcore-incomplete`
(`providers/local_libvirt/retrieve/guestfs.py`), so the unit writes
`/var/crash/%b/vmcore-incomplete` — `%b` being the systemd boot ID, unique to the capture boot —
and renames to `vmcore` only on success. A flat `/var/crash/vmcore` is at depth 1, matches
neither glob, and would present a successful capture as `readiness_failure`. A fixed path would
also collide with the previous core on a second crash against the same overlay. `poweroff` runs
after a `;` rather than a `&&`, so a failed capture still reaches SHUTOFF — the signal the
harvest waits on — instead of burning the full settle timeout.

**One authoritative copy.** The unit lives at
`deploy/remote-libvirt-guest-helpers/fadump-capture.service`. Both paths upload that same file:
the build-fs path resolves it from the source tree the way `drgn_helper_steps()` already does,
and the Ansible role uploads the copy `main.yml` already stages onto the remote build host. The
role must not read it from `role_path` — `virt-customize` resolves `--upload` on the host it
runs on (the remote), and `role_path` is a control-node path.

**build-fs path:** `rhel.py` emits the upload + enable inside the `kexec-tools` guard, gated on
`ctx.fadump_capture` — the arch's `fadump_capture` trait, resolved by the build plane from
`spec.arch` the way `console_device` already is. True on ppc64le, where firmware fadump re-boots
the real rootfs with `/proc/vmcore` present; False on x86_64, where the kdump capture kernel
stays inside its dracut initramfs and never loads `/etc/systemd/system`.

**Ansible role path:** Inject conditionally via the `fadump_capture: true` per-image boolean,
using `virt-customize --upload` + `--run-command`. The boolean defaults to `false` so existing
images are unchanged.

**Rejected: install unconditionally on every kdump-capable image** — the justification was that
`ConditionPathExists` makes it a safe no-op everywhere. It is a no-op on x86_64 for a different
reason (the unit is never loaded at all), so an unconditional install bakes a declaration the
image cannot honor. The arch trait is one table row, not a `RootfsCatalogEntry` schema change.

## Fix 3 — the harvest must mount more than the root filesystem

Found by running the arc live (see Proof below), not by review. `_LibguestfsCoreReader._mount`
mounted only `inspect_os()[0]` at `/`. Fedora Cloud images — the ppc64le fadump images this
harvest exists to read — put `/var` on its own btrfs subvolume, so `/var/crash` was EMPTY under
a root-only mount and a real captured vmcore was invisible. Fixes 1 and 2 are necessary but not
sufficient: the unit and the globs can agree perfectly and still yield `readiness_failure`.

`inspect_get_mountpoints(root)` already reports the full layout
(`/`, `/boot`, `/home`, `/var`); the harvest now mounts all of it, shortest path first so a
parent precedes its child. The root mount stays fatal — an overlay whose root will not mount is
unreadable, and that must remain a typed `INFRASTRUCTURE_FAILURE` rather than an empty core list
reading as "the guest never dumped". Non-root mounts are best-effort and logged, so one
unreadable `/home` cannot deny a core sitting on a healthy `/var`.

## Fix 2 — bundle layout

Fedora 44 RPMs include `lib/modules/<rel>/vmlinuz` (~63 MiB). Together with `boot/vmlinuz`
(~63 MiB) this pushes the first `.ko.xz` past `_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB). The fix
is documentation: add `--exclude='lib/modules/*/vmlinuz'` to the bundle-build instructions in
`docs/operating/runbooks/live-testing.md`. No code change needed.

## Acceptance criteria

1. `RhelFamily.customize_steps()` injects `fadump-capture.service` when `kexec-tools` is present
   **and** the arch carries the `fadump_capture` trait; a non-fadump arch gets no unit.
2. `guest_base_image` role injects the unit for images with `fadump_capture: true`, uploading
   from the remote-staged helper directory rather than `role_path`.
3. Every path the unit writes matches a glob the local-libvirt harvest lists, staged through
   `vmcore-incomplete` and unique per capture boot.
4. `just lint`, `just type`, `just lint-ansible`, `just test-ansible` pass.
5. Runbook documents `--exclude='lib/modules/*/vmlinuz'` for the ppc64le bundle.
6. The offline harvest mounts every filesystem the guest declares, so a core on a separate
   `/var` is listed; a root mount that fails still raises `INFRASTRUCTURE_FAILURE`.

## Proof

Run on emulated POWER10 (`qemu-system-ppc64 -machine pseries,accel=tcg`, Fedora 43 ppc64le),
which exposes the `ibm,configure-kernel-dump` RTAS token, so firmware-assisted dump is real
here: `fadump: Reserved 1024MB`, `rtas fadump: Registration is successful!`.

`echo c > /proc/sysrq-trigger` → `Kernel panic - not syncing: sysrq triggered crash` →
`rtas fadump: Firmware-assisted dump is active.` → `Reserving 7168MB of memory ... for
preserving crash data` → the unit ran on the capture boot and `reboot: Power down`.

The overlay then held `/var/crash/766aec1beea44559be54678d0f642503/vmcore` — the boot-ID
directory, renamed from `vmcore-incomplete`, 43,707,444 bytes,
`Kdump compressed dump v6 ... machine ppc64le`. The production `_LibguestfsCoreReader`
listed it: one entry, `incomplete=False`. Before Fix 3 the same reader on the same overlay
returned zero entries.

Fix 1's install source was proved separately on a real remote build host: after the role's
staging step, the post-fix source `/root/kdive-image-build/helpers/fadump-capture.service`
exists there and the pre-fix `role_path` source does not.
