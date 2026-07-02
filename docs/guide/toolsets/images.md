# images toolset

A system provisions from a base **image** — the guest rootfs (and its baked toolchain). The
image you pick decides what the guest can do out of the box, so choose it before you
provision: a multi-kernel or non-kdump image can burn an allocation on a capability the run
needs. Reach for these to pick an image and read its capabilities first. For exact parameters,
types, and return schema, read each tool's own description.

## Picking an image

- `images.list` — the RBAC-filtered catalog view: the images you can see, to choose from.
- `images.describe` — the full detail for one image: boot layout, `package_versions`, and the
  computed `capability_signals`. **Call this before `systems.provision`.** Two signals matter
  most:
  - `kdump` — whether the image can capture a vmcore for a target kernel (the crash-triage
    path depends on it).
  - `direct_kernel` — `provisionable` only when `/boot` holds exactly one non-rescue kernel;
    a multi-kernel image reads `not_provisionable`/`unverified`, so a direct-kernel provision
    would fail closed. Read it first so a multi-kernel image does not waste an allocation.

## debug vs build images

The families ship two rootfs flavors for different jobs:

- a **debug/guest** image carries the in-target crash and introspection toolchain — `crash`,
  drgn, `kdump-tools`/`makedumpfile`, and `openssh-server` — for booting the kernel under test
  and inspecting it.
- a **build-host** image carries the kernel-build toolchain (compiler, `make`, headers,
  `pahole`) for the server-build lane.

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
