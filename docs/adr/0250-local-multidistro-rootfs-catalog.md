# ADR 0250 — Local multi-distro rootfs catalog + kdump incomplete-core disclosure

- **Status:** Accepted
- **Date:** 2026-06-25
- **Issue:** [#817](https://github.com/randomparity/kdive/issues/817)
- **Spec:** [`../superpowers/specs/2026-06-25-local-multidistro-rootfs-catalog-817.md`](../superpowers/specs/2026-06-25-local-multidistro-rootfs-catalog-817.md)
- **Depends on:** [ADR-0092](0092-image-rootfs-lifecycle.md) (the `RootfsBuildPlane`/`RootfsBuildSpec`
  contract this extends), [ADR-0188](0188-ansible-image-catalog.md) (the remote image catalog whose
  per-distro shape this mirrors for the local rootfs), [ADR-0244](0244-per-run-vmcore-capture.md)
  (the Run-owned vmcore capture the incomplete-core handling reports through).

## Context

On local-libvirt, `vmcore.fetch` with the default `kdump` method returns `READINESS_FAILURE`
"no complete core appeared within the capture window" for a from-source kernel, while explicit
`host_dump` succeeds (#817). Live diagnosis on the KVM host pinned the cause: the ready rootfs
(`fedora-kdive-ready-43.qcow2`) ships **makedumpfile 1.7.8** (`LATEST_VERSION = 6.17.4`), which
cannot filter a kernel **7.0.0** vmcore (`-d 31`) — it prints "The kernel version is not supported"
and leaves `/var/crash/<ts>/vmcore-incomplete`. The host harvest glob `/var/crash/*/vmcore` never
matches that name, so the capture reports no core. Everything else is healthy (crashkernel reserves,
`kdumpctl` arms, capture kernel boots and powers off; the baseline Fedora kernel captures a complete
core). makedumpfile **1.7.9** (2026-04-20) is the first release supporting kernels up to v7.0, and
ships in **Fedora 44**.

Two problems compound: the image's toolchain is too old for newer kernels, and the local rootfs
build is hardcoded to one Fedora image sourced only via `virt-builder` (whose template repo has no
`fedora-44`). The user's goal is broader: a multi-distro rootfs matrix (Fedora, Rocky, CentOS
Stream, Debian, openSUSE) exercising the lifecycle across base OSes, with #817 as the MVP slice.

## Decision

Replace the single hardcoded Fedora rootfs build with a declarative, file-authoritative
multi-distro catalog and a per-family customizer seam, source bases from either a `virt-builder`
template or a sha256-pinned cloud-image URL, ship Fedora 44 as the kdump-capable default, and make
the kdump harvest disclose an incomplete core honestly.

1. **Declarative catalog.** `fixtures/local-libvirt/rootfs_catalog.toml` lists each image
   (`name`, `distro`, `version`, `family`, `arch`, `kind`, `source`). `images/rootfs_catalog.py`
   loads/validates it and resolves `build-fs --image <name>`; it replaces `images/distros.py`.

2. **Dual base sourcing.** `images/base_source.py` acquires the scratch base via
   `virt-builder <template>` or by downloading a cloud-image `url` and verifying its `sha256`
   (fail-closed `CONFIGURATION_ERROR` on mismatch). The existing `virt-tar-out`/`virt-make-fs`
   repack rebuilds the bare ext4 whole-disk rootfs regardless of the source partition layout.

3. **Family customizer seam.** `images/families/` defines a `FamilyCustomizer` protocol
   (`packages`, `customize_argv`, `normalize`). The MVP ships `rhel` (Fedora, Rocky, CentOS
   Stream); the existing inline Fedora customization moves into it. `debian` and `suse` are added
   by follow-up issues — the protocol exists now so those PRs are additive. A structured
   `kdump_capable` capability flag is deferred (YAGNI) to the first follow-up that renders it; the
   makedumpfile-vs-kernel limitation is conveyed at runtime by the incomplete-core remediation.

   The cloud-image lane is **not** assumed equivalent to the virt-builder scratch: a Fedora Cloud
   base is btrfs-with-subvolumes + separate `/boot`/ESP + cloud-init, so the lane disables
   cloud-init, collapses the source to one bare ext4 via the existing `virt-tar-out`/`virt-make-fs`
   repack, and SELinux-relabels the result. This path is proven by a manual spike before the
   abstraction is generalized (it was never exercised in diagnosis).

4. **Fedora 44 default.** Add `fedora-kdive-ready-44` (cloud-image source), retain
   `fedora-kdive-ready-43` (regression reference), and register the new image in the inventory the
   same way 43 is registered. Live-prove a complete 7.0 vmcore via the default `kdump` method.

5. **Incomplete-core handling.** `_LibguestfsCoreReader.list_vmcores` also globs
   `/var/crash/*/vmcore-incomplete`. A complete `vmcore` is still preferred and an incomplete core
   is never promoted; when only an incomplete core exists, `capture` raises `READINESS_FAILURE`
   with `reason="kdump_core_incomplete"` and a drift-proof, **cause-neutral** remediation constant
   pointing at `host_dump` or a newer image. Cause-neutral because `vmcore-incomplete` is also the
   transient mid-save name, so on the harvest timeout path it may name a slow (not toolchain-old)
   capture; the remediation names both causes. The genuinely-empty case keeps `_no_core`.

## Consequences

- The default local kdump path captures a complete core for a current from-source kernel on
  Fedora 44; #817's headline symptom is fixed at the root cause (the toolchain), not masked.
- Adding a base OS is a catalog row plus, for a new packaging family, one `FamilyCustomizer` — no
  changes to the build pipeline, repack, or inventory wiring.
- A distro whose makedumpfile is older than the kernel-under-test still produces an incomplete
  core; the worker now returns a clear, actionable failure naming `host_dump`/newer-image rather
  than the opaque window-timeout message.
- The contract stays deny-nothing: an operator can still request kdump on an old-toolchain image
  and gets the disclosed incomplete-core remediation rather than a silent failure or a hard gate.
- Bit-reproducible rootfs rebuilds remain a non-goal (ADR-0092); the falsifiable provenance now
  records the pinned cloud-image `url@sha256` or `virt-builder` template per image.

## Considered & rejected

- **Pin the kernel-under-test to makedumpfile's supported range** — defeats the purpose of
  debugging arbitrary from-source kernels.
- **Widen the capture window or promote `vmcore-incomplete`** — masks a truncated, unreliable dump
  as success; the newer makedumpfile is the real fix.
- **Code registry in `distros.py`** — adding an image becomes a code change, drifting from the
  file-authoritative catalog convention used by build-configs and the image catalog.
- **Unify onto the ansible `kdive_image_catalog`** — that catalog is host-inventory (group_vars,
  remote full-disk images), not app-level, and local needs the bare-ext4 repack. Reuse the shape,
  not the file.
- **Fedora 44 only, no catalog** — leaves the next distro a one-off again; the goal is a reusable
  matrix (epic; #817 is the MVP slice).
