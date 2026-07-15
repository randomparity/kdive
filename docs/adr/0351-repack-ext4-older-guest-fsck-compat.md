# ADR 0351 — Repack a whole-disk ext4 an older-guest e2fsck can check: strip the 1.47-only `orphan_file`

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1152 (surfaced by the CentOS Stream 9 ppc64le customize-boot proof)
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0345 (#1147, unified customization boot), ADR-0030/0272 (no-partition-table
  whole-disk ext4 + direct-kernel boot)

## Context

The customization boot (ADR-0345) repacks the customized root tree into a no-partition-table
whole-disk ext4 qcow2 with `virt-make-fs --type=ext4`, boots it once to self-customize, then
provisions it. The repack runs inside the **libguestfs appliance**, which is built from the build
host (Fedora 44, e2fsprogs **1.47.3**). That mke2fs stamps `orphan_file` (and `metadata_csum_seed`)
into the ext4 by default.

`orphan_file` is an e2fsprogs **1.47** feature (2024). The guest's `systemd-fsck-root.service` runs
the **guest's own** e2fsck against `/dev/vda` at boot (the family `FSTAB` is
`/dev/vda / ext4 defaults 0 1` — fsck passno 1). On a distro whose userspace predates 1.47 — every
**EL ≤ 9** guest (RHEL/CentOS Stream/Rocky 9 ship e2fsprogs 1.46.5) — that e2fsck rejects the
unknown `orphan_file` feature, `systemd-fsck-root` fails, `/sysroot` cannot mount, and the guest
drops to **emergency mode**. The customization firstboot never runs.

This was latent: #1147 proved the mechanism only on **Fedora**, whose guest e2fsck (1.47) matches
the appliance and accepts `orphan_file`. The #1152 CentOS Stream 9 ppc64le proof is the first EL9
customize-boot, and it hit the emergency-mode fsck failure. The defect is **distro-userspace-version
skew, not arch** — it bites x86_64 EL9 guests identically; ppc64le only surfaced it first.
(`metadata_csum_seed` is a 1.43 feature EL9's e2fsck supports, so only `orphan_file` is
incompatible.)

## Decision

**The repack produces a whole-disk ext4 whose feature set an older-guest e2fsck can check, by
building the filesystem exactly as before and then stripping the 1.47-only `orphan_file` feature
before the qcow2 the provider boots is produced.**

`_real_repack_whole_disk_ext4` becomes: `virt-tar-out` → `virt-make-fs --type=ext4 --format=raw`
(the proven construction, to a raw image) → `tune2fs -O ^orphan_file <raw>` (strip the incompatible
feature; a host e2fsprogs metadata operation on the image file, arch-neutral — no guest code runs)
→ `qemu-img convert -f raw -O qcow2` (to the qcow2 the direct-kernel boot consumes). The disabled
feature is a single named constant with a comment tying it to the EL9 e2fsck floor.

`virt-make-fs`'s construction and sizing are unchanged; the fix is one surgical strip plus a
format conversion, keeping the proven primitive rather than replacing it with a hand-rolled
guestfish `mkfs`/`mount`/`tar-in` sequence.

## Consequences

- Every customize-booted image — every arch, every family — passes the guest's `systemd-fsck-root`
  on EL ≤ 9 as well as on Fedora/EL10; the Fedora path is behaviorally unchanged (its guest e2fsck
  accepted `orphan_file` anyway, and now simply never sees it).
- The non-Fedora ppc64le customize-boot proof (CentOS Stream 9, #1152) can reach userspace.
- The repack gains a transient raw intermediate (the fs size, default 6G) and two host tools,
  `tune2fs` (e2fsprogs) and `qemu-img` (qemu) — both already transitively required by the libguestfs
  + libvirt/QEMU build path, so no genuinely new dependency; a missing binary surfaces the existing
  categorized "cannot build the rootfs image" error.
- No migration, no schema change, no change to any catalog row or to the boot/provision path.

## Rejected alternatives

- **Replace `virt-make-fs` with a guestfish `mkfs ext4 … features:^orphan_file` + `mount` +
  `tar-in` sequence.** Verified to work (guestfish `mkfs` exposes a `features` optarg → mke2fs
  `-O`) and avoids the raw intermediate, but it discards the proven `virt-make-fs` construction and
  its sizing for a hand-rolled multi-step script, a larger regression surface on a path every build
  uses. The surgical strip keeps the proven primitive.
- **Strip the feature with a privileged `qemu-nbd` + host `tune2fs` on the qcow2.** Rejected:
  `qemu-nbd` needs the `nbd` module and root, a new privileged dependency; `tune2fs` on the raw
  image file needs neither.
- **Also strip `metadata_csum_seed`.** Rejected as unnecessary: it is a 1.43 feature EL9's 1.46.5
  e2fsck supports; only `orphan_file` is the 1.47 incompatibility. Stripping more than the defect
  requires would diverge the filesystem from the distro default for no benefit.
- **Pin the appliance's mke2fs defaults / pass `-O` through `virt-make-fs`.** Rejected: the
  appliance's mke2fs.conf is not ours to change, and `virt-make-fs` exposes no mke2fs feature
  passthrough.
- **Fall back to an EL10/Fedora proof and defer the fix (the spec's original risk-1 fallback).**
  Rejected by the issue owner: the fix is a small, arch-agnostic correctness change that unblocks
  every EL ≤ 9 customize-boot (x86_64 included), so it is worth doing where it was found rather than
  shipping a catalog whose EL9 rows cannot customize-boot.

## Rollout

Additive and backward compatible. No migration; the Fedora/EL10 paths are unchanged (they never
depended on `orphan_file`), and EL ≤ 9 images now pass guest fsck. Proven by the #1152 CentOS
Stream 9 ppc64le live TCG customize-boot.
