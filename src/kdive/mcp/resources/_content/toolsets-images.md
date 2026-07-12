# images toolset

A system provisions from a base **image** — the guest rootfs (and its baked toolchain). The
image you pick decides what the guest can do out of the box, so choose it before you
provision: a multi-kernel or non-kdump image can burn an allocation on a capability the run
needs. A capability signal that reads `unverified` is not a failure — it is the honest state for
an externally-baked image no one has characterized yet (see the `direct_kernel` note below).
Reach for these to pick an image and read its capabilities first. For exact parameters,
types, and return schema, read each tool's own description.

## Picking an image

- `images.list` — the RBAC-filtered catalog view. Each row carries enough to **compare images
  on merit in one call**, so you rarely need an `images.describe` per candidate:
  - `capabilities` — the build-fact tags baked into the image. Match the tag to the job:
    `kdump` for crash/vmcore work, `drgn`/`agent` for live introspection, `ssh` for guest
    access, `build` for a kernel-build host. A tag means the tooling is present, not that a
    feature is live end to end (that is what the signals below verify).
  - `os` — the verified base OS identity (`id`, and `version_id` when known), read from the
    built image itself. Use it to match the target distro/release.
  - `default_kernel_version` — the kernel the image ships and boots by default (`""` when
    unknown). Use it to know what version you are starting from before building your own.
  - `description` — an optional operator-attested hint about what an image is for. Advisory
    context only, never a capability guarantee — verify with the signals below.
  Do not just reuse the image named in a `systems.profile_examples` example: that one is picked
  by declaration order, and the example's `selection_note`/`available_images` say so.
- `images.describe` — the full detail for one image: boot layout, `package_versions`, `os`,
  `description`, and the computed `capability_signals`. **Call this before `systems.provision`.**
  Three signals matter most:
  - `kdump` — whether the image can capture a vmcore for a target kernel (the crash-triage
    path depends on it).
  - `direct_kernel` — `provisionable` only when `/boot` holds exactly one non-rescue kernel;
    a multi-kernel image reads `not_provisionable`, so a direct-kernel provision would fail
    closed. Read it first so a multi-kernel image does not waste an allocation.
  - `live_drgn` — `capable` only when the image's shipped drgn is new enough to introspect a
    booted kernel from the guest's own in-guest BTF (`/sys/kernel/btf`); an image on an older
    drgn reads `incapable`. Read it before provisioning for live introspection so an image whose
    drgn cannot see the kernel does not waste an allocation.

  A signal reads `unverified` when its operand was never recorded — the **normal, honest state**
  for an externally-baked (`s3`) or operator-staged image that no one has characterized. It is not
  a defect and does not block provisioning; it just means the pre-check cannot answer yet. The
  check becomes actionable once the operand is recorded — either KDIVE built/published the image,
  or the operator **attested** it in `systems.toml` (`[image.attested]`). When an operand is
  present, `basis` says how it is known: `build_verified` (a KDIVE build) or `operator_attested`
  (an operator claim kdive did not verify, also flagged by `provenance_attested`).
- `images.kernel_config` — a short-lived download URL for the image's own `/boot/config-<ver>`.
  Use it as a **known-good starting `.config`** when you build a kernel locally: it already
  boots this image. kdive never validates the config you build from it. An image with no
  offered config returns `kernel_config_unavailable`.

## debug vs build images

The families ship two rootfs flavors for different jobs:

- a **debug/guest** image carries the in-target crash and introspection toolchain — `crash`,
  drgn, `kdump-tools`/`makedumpfile`, and `openssh-server` — for booting the kernel under test
  and inspecting it.
- a **build-host** image carries the kernel-build toolchain (compiler, `make`, headers,
  `pahole`). You build kernels locally and upload them (see
  [Build lane](../../operating/external-build-upload.md)), so this flavor is only useful as a
  ready-made toolchain guest, never as a platform build target.

`images.describe`'s `package_versions` shows what a given image actually baked in.

## Extending an image at runtime

You do **not** need a bespoke image for every missing tool. Once a system is ready you have
root in the guest and the guest package manager is yours — install what the run needs at
runtime (`apt install trace-cmd`). See "The guest is yours" in the investigation index. Pick
the closest base image, then extend it live.

## Managing the catalog (operator)

These change the catalog and are gated to operators/admins:

- `images.upload` — register a quarantined upload as a project-private image.
- `images.build` — enqueue a platform image build.
- `images.publish` — publish a built image to the catalog.
- `images.delete` — delete a project-private image.
- `images.extend` — re-arm a private image's retention `expires_at` (break-glass); this
  extends the image's *lifetime in the catalog*, not its installed packages.
- `images.prune_expired` — force the expired-private-image retention sweep now.
