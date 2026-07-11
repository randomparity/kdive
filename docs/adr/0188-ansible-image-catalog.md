# ADR 0188 — A selectable, multi-image rootfs catalog in the ansible layer

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers
- **Refines:** the image decision in `docs/archive/superpowers/specs/2026-06-18-ansible-remote-libvirt-host-setup-design.md` ("Guest image distro: Fedora-pinned"); composes with [ADR-0183](0183-provider-aware-platform-root-cmdline.md) (`root_device`)
- **Issue:** #598
- **Spec:** `docs/archive/superpowers/specs/2026-06-19-ansible-image-catalog-design.md`

## Context

`deploy/ansible/site.yml` configures a remote-libvirt host (libvirt + mutual-TLS + storage
pool + gdbstub ACL) but stages **no** rootfs image, so a freshly-brought-up host cannot
provision a System — the provisioner clones a per-System overlay from a staged base image,
and `/var/lib/libvirt/images/` is empty. The opt-in `playbooks/image.yml` builds exactly
**one** image from single-valued globals (`base_image_distro` / `base_image_version` /
`base_image_name` / `base_image_source`). We want a selectable, expandable catalog: each
host declares which image(s) to stage, the catalog grows beyond Fedora, and re-runs stage
only missing/changed images.

The app side already supports many images: `inventory/model.py` models
`image: list[ImageEntry]` with a discriminated `source` union and enforces image-identity
uniqueness `(provider, name, arch)` and `base_image` reference integrity. A System selects
a staged image by volume name at provision time. So the change is entirely in the ansible
layer; the emitted `systems.toml` must still pass `InventoryDoc.parse`.

The provider's contract assumes an in-guest `qemu-guest-agent` (the `exec_with_capability`
artifact channel and `boot_id`-change readiness), the three helpers, and `curl`/`tar` (the
install channel downloads the kernel bundle). A pure busybox image with no agent cannot
satisfy that path.

## Decision

1. **Catalog as data.** Replace the single-image globals with a `kdive_image_catalog` list
   of image-definition dicts in `group_vars/all.yml`, plus a `kdive_image_defaults` map for
   the shared package/helper sets and toggles. Each entry carries a logical `name`,
   `distro`/`version`, `source` (`virt-builder` | `cloud-image` | `scratch`), and a
   `root_device`, and overrides only the defaults it differs on.

2. **Per-host selection.** `host_vars/<host>.yml` declares `host_images` (catalog names) and
   an optional `host_default_image` (defaults to `host_images[0]`). The `guest_base_image`
   role loops over the selected entries; `remote_libvirt_facts` emits one `[[image]]` block
   per selected entry and names `host_default_image` as `[[remote_libvirt]].base_image`. The
   role fails fast if a selected name is absent from the catalog or its `arches` excludes
   `ansible_architecture`.

3. **Catalog ships fedora, ubuntu, rocky, and a bare image.** fedora keeps its native
   `virt-builder` path; ubuntu/rocky use the `cloud-image` path (download + `virt-customize`);
   the bare image uses a new `scratch` path.

4. **Bare image is "bare but conformant" (Option A).** The bare image still runs
   `qemu-guest-agent`, the family-appropriate helpers, and `curl`/`tar` under a systemd init,
   so the build→install→boot→debug arc is unchanged on a Fedora/RHEL host and no provider work
   is needed. It is built **from the host OS family** (`dnf --installroot` on RedHat,
   `debootstrap` on Debian) into a minimal rootfs assembled into a partitioned bootable qcow2
   via guestfish.

5. **The helper/package contract is Fedora/RHEL-family; Ubuntu's install arc is scoped out.**
   The three in-guest helpers are a Fedora/RHEL reference implementation (`grubby` / `dracut`
   / `grub2-reboot` / `kdump-utils`), and `kdive_image_defaults.packages` is a Fedora/RHEL
   set. So the full kdive arc is delivered for **fedora** and **rocky** (both RHEL-family);
   the **ubuntu** image stages, boots, and connects its guest agent (provision / `host_dump`
   path) but `runs.install` (the `kdive-install-kernel` `grubby` path) does **not** work until
   a Debian helper variant lands — tracked separately. The ubuntu entry carries its own Debian
   package set (`kdump-tools`, no `drgn`) because `virt-customize --install` runs the image's
   own package manager. This is an honest scoping of the prior Fedora pin, not a claim that the
   Fedora helpers run on Debian.

6. **`root_device` is catalog metadata for remote.** Each entry declares a `root_device`
   (default `/dev/vda`); the facts template emits it verbatim. The remote provider sets
   `platform_root_cmdline=None` and never consumes `root_device` on the boot/install path —
   the in-guest GRUB owns the real root (ADR-0183, #587). So the value is the `[[image]]`
   schema's required metadata, best-effort and confirmed on hardware, never a fabricated
   partition number that could mislead.

## Consequences

- A host bring-up + `playbooks/image.yml` stages each `host_images` entry idempotently; two
  hosts can stage different sets. Multiple `[[image]]` blocks register per host; an agent
  selects any staged image per System via `base_image_volume`.
- No `src/kdive/**` change. A new contract test renders a representative four-image catalog
  to `systems.toml` and asserts `InventoryDoc.parse` accepts it (the ansible→app seam).
- The single-image globals (`base_image_distro`/`_version`/`_name`/`_source`) are removed,
  not deprecated; `playbooks/image.yml`, the `guest_base_image` role, and the facts template
  read the catalog. (Replace, don't deprecate.)
- The `scratch`/bare build path and ppc64le remain **unvalidated** (no host in CI or
  available hardware), caveated like the existing ppc64le path. Multi-user boot of each
  non-bare image and the Option-A bare contract are hardware-only acceptance checks.

## Considered & rejected

- **A first-class image entity or any app change.** The app already models a list; the gap
  is ansible-only.
- **Option B bare image** (no agent; console/gdbstub-only readiness) — needs a provider
  console-readiness path; tracked separately.
- **Wiring image build into `site.yml`** — image build stays the deliberately-separate,
  slow, opt-in `playbooks/image.yml` (issue author's correction).
- **A task file per distro** — one `build_one.yml` dispatching on `image.source` keeps the
  per-image flow together; only the genuinely different scratch assembly splits out.
- **Per-entry full package lists** — `kdive_image_defaults` holds the shared set.
