# Design: A selectable, multi-image rootfs catalog in the ansible layer

**Status:** Proposed
**Date:** 2026-06-19
**Issue:** #598
**ADR:** [0188](../../adr/0188-ansible-image-catalog.md)
**Supersedes the image decision in:** `docs/superpowers/specs/2026-06-18-ansible-remote-libvirt-host-setup-design.md`
("Guest image distro: Fedora-pinned regardless of host distro/arch")

## Problem

`deploy/ansible/site.yml` brings a remote-libvirt host up to the point of serving a
`qemu+tls` listener with a storage pool, network, and gdbstub ACL, but stages **no**
rootfs image. `/var/lib/libvirt/images/` is empty after bring-up, so no host can
provision a System (the provisioner clones a per-System overlay from a staged base
image). The opt-in `playbooks/image.yml` builds exactly **one** image from single-valued
globals (`base_image_distro` / `base_image_version` / `base_image_name` /
`base_image_source` in `group_vars/all.yml`).

We want each server to declare which image(s) to stage, the catalog to grow beyond
Fedora (Ubuntu, Rocky, a minimal **bare** image), and re-runs to stage only
missing/changed images.

## The app side already supports many images — the gap is entirely ansible

`src/kdive/inventory/model.py` already models `image: list[ImageEntry]` with a
discriminated `source` union (`S3Source | BuildSource | StagedSource`) and enforces two
invariants the ansible-emitted catalog must satisfy:

- **image identity `(provider, name, arch)` is unique** (`_check_image_identities`);
- **every `[[remote_libvirt]].base_image` names a declared `[[image]]`**
  (`_check_base_image_refs`).

A System selects a staged image at provision time by volume name
(`profiles/provisioning.py` `base_image_volume`). So a multi-image catalog needs **no app
changes** — it is purely an ansible-layer refactor that must keep emitting a `systems.toml`
the app's `InventoryDoc.parse` accepts.

## Scope

**In scope:** replace the single-image globals with a `kdive_image_catalog` (data),
per-host `host_images` selection, a `guest_base_image` role that loops over the selected
definitions with a per-source build path (`virt-builder` / `cloud-image` / `scratch`),
per-image idempotency, and a `remote_libvirt_facts` role that emits one `[[image]]` block
per staged image plus the host's default `base_image`. Catalog ships fedora (existing),
ubuntu, rocky, and a bare image.

**Out of scope:** any `src/kdive/**` change; the provider's console-only no-agent boot path
(issue's "Option B" bare image — tracked separately); wiring image build into `site.yml`
(image build stays the deliberately-separate, slow `playbooks/image.yml` step per the issue
author's correction).

## Decisions

| Axis | Decision | Rationale |
|---|---|---|
| Catalog shape | A `kdive_image_catalog` list of image-definition dicts in `group_vars/all.yml`, with a `kdive_image_defaults` map for shared fields (package/helper sets). Each entry overrides only what differs. | "Image catalog as data" (issue goal 1); defaults keep entries terse without a per-entry copy of the package list. |
| Per-host selection | `host_images` (list of catalog names) in `host_vars/<host>.yml`; `host_images` default in `group_vars`; `host_default_image` (defaults to the first of `host_images`) names the `[[remote_libvirt]].base_image`. | "Per-server selection" (goal 2); two hosts stage different sets. |
| Selection validation | The role fails fast if a `host_images` name is absent from the catalog, or if a selected image's `arches` does not include `ansible_architecture`. | A typo or arch mismatch must fail at bring-up, not silently stage nothing or stage an unbootable image. |
| Bare image contract | **Option A — bare but conformant** (issue recommendation): the bare image still runs `qemu-guest-agent`, the three helpers, and `curl`/`tar`, with a systemd init, so the full build→install→boot→debug arc is unchanged. No provider work. | The provider's readiness/exec/install channels require the agent + helpers + curl/tar; a no-agent image (Option B) needs provider work and is tracked separately. |
| Bare image build | A new `scratch` source kind built **from the host OS family** (`dnf --installroot` on RedHat, `debootstrap` on Debian) into a minimal rootfs (systemd + busybox + agent + helpers + curl/tar + kernel + grub), assembled into a partitioned bootable qcow2 via guestfish. | busybox/bare has no virt-builder template; building from the host's own package manager keeps the userland family-consistent and the systemd contract intact. |
| `root_device` per image | Each catalog entry declares its real `root_device` (e.g. partitioned cloud images use `/dev/vda<n>` / the image's UUID root, not whole-disk `/dev/vda`); the facts template emits it verbatim. | ADR-0183 (#587): the platform no longer injects `root=` for remote, so each image owns its real root device/partition layout. |
| Sources | `virt-builder` (fedora, native), `cloud-image` (ubuntu, rocky — downloadable qcow2 + `virt-customize`), `scratch` (bare). | The three realization paths the catalog needs; ubuntu/rocky ship cloud images, fedora keeps its virt-builder template. |
| Build-host prereqs | `guest_image_prereqs` already covers the Debian appliance fixes; the scratch path additionally needs `debootstrap` (Debian) present, asserted in the role. | The prereqs role must cover each host family that builds images (issue consideration). |

### Considered & rejected

- **A first-class image entity / app change.** Rejected — the app already models a list;
  the gap is ansible-only.
- **Option B bare image (no agent, console/gdbstub-only readiness).** Rejected for this
  cut — needs a provider console-readiness path (larger change); tracked separately.
- **Wiring image build into `site.yml`.** Rejected per the issue author's correction —
  image build is a deliberately separate, slow, opt-in step (`playbooks/image.yml`).
- **A separate task file per distro.** Rejected — one `build_one.yml` dispatching on
  `image.source` keeps the per-image flow in one place; only the genuinely different
  scratch assembly is split into `build_scratch.yml`.
- **Per-entry full package lists.** Rejected — `kdive_image_defaults` holds the shared set;
  entries override only deltas.

## Catalog data model (`group_vars/all.yml`)

```yaml
kdive_image_defaults:
  packages: [qemu-guest-agent, drgn, kexec-tools, makedumpfile, kdump-utils,
             curl, tar, openssl, python3]
  helpers: [kdive-install-kernel, kdive-capture-vmcore, kdive-drgn]
  include_kernel_debuginfo: false
  crashkernel: "256M"
  arches: [x86_64]

kdive_image_catalog:
  - name: fedora-kdive-remote-base-43
    distro: fedora
    version: "43"
    source: virt-builder
    root_device: /dev/vda3            # partitioned cloud base, XFS root (ADR-0183)
  - name: ubuntu-2404-kdive-remote-base
    distro: ubuntu
    version: "24.04"
    source: cloud-image
    cloud_image_url: "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-{{ image_arch_alias }}.img"
    root_device: /dev/vda1
  - name: rocky-10-kdive-remote-base
    distro: rocky
    version: "10"
    source: cloud-image
    cloud_image_url: "https://dl.rockylinux.org/pub/rocky/10/images/{{ ansible_architecture }}/Rocky-10-GenericCloud-Base.latest.{{ ansible_architecture }}.qcow2"
    root_device: /dev/vda5
  - name: bare-kdive-remote-base
    distro: bare
    version: "1"
    source: scratch
    root_device: /dev/vda1

host_images_default: [fedora-kdive-remote-base-43]
```

Each entry resolves its effective `packages` / `helpers` / `include_kernel_debuginfo` /
`crashkernel` / `arches` via `item.<field> | default(kdive_image_defaults.<field>)`.

Per-host (`host_vars/<host>.yml`):

```yaml
host_images: [fedora-kdive-remote-base-43, ubuntu-2404-kdive-remote-base, bare-kdive-remote-base]
host_default_image: fedora-kdive-remote-base-43   # optional; defaults to host_images[0]
```

## Role flow (`guest_base_image`)

`main.yml`:
1. Resolve `guest_base_image_selected` = catalog entries whose `name in host_images`.
2. Assert every name in `host_images` is present in the catalog (fail fast on typo).
3. Assert every selected entry's effective `arches` includes `ansible_architecture`.
4. `include_tasks: build_one.yml` over `guest_base_image_selected` (loop_var `image`).

`build_one.yml` (per image, idempotent on `<name>.qcow2` present unless
`force_image_rebuild` or per-image `image.force | default(false)`):
- `stat` the staged volume → skip the build block when present and not forced.
- Dispatch on `image.source`: `virt-builder` / `cloud-image` (existing two paths,
  parameterized by `image`) / `scratch` (`include_tasks: build_scratch.yml`).
- Install the per-image helper set, stage into the pool (root-owned), refresh the pool,
  record the sha256.

`build_scratch.yml` (bare, from host family — **unvalidated**, like ppc64le):
- RedHat family: `dnf --installroot` a minimal set (systemd, busybox, qemu-guest-agent,
  kexec-tools, makedumpfile, curl, tar, kernel, grub2, the helpers).
- Debian family: `debootstrap` the equivalent set (busybox via the busybox package).
- Assemble the root tree into a partitioned bootable qcow2 via guestfish (mkfs, copy-in,
  install grub, write `/etc/fstab` with the root UUID), enable `qemu-guest-agent`.

## Facts emission (`remote_libvirt_facts`)

`systems_toml_block.j2` loops over the host's selected images and emits one `[[image]]`
block each (`provider="remote-libvirt"`, `name`, `arch=ansible_architecture`,
`format="qcow2"`, `root_device=<entry.root_device>`, `visibility="public"`,
`[image.source] kind="staged" volume="<name>.qcow2"`). `[[remote_libvirt]].base_image`
is `host_default_image | default(host_images[0])`. The role must reuse the same
selection/validation facts the build role uses so the emitted catalog matches what was
staged.

## Verification

- `just lint-ansible` (yamllint + ansible-lint + `ansible-playbook --syntax-check` on
  `site.yml`, `playbooks/pki.yml`, `playbooks/image.yml`) green.
- **Contract test** (`tests/inventory/test_image_catalog_contract.py`): render a
  representative four-image (fedora/ubuntu/rocky/bare) `systems.toml` for a host and assert
  `InventoryDoc.parse` accepts it — identities unique, `base_image` resolves, each source
  is a valid `staged` source. This is the one behavioral guardrail CI can enforce for the
  ansible→app seam.
- **Hardware-only (documented, not CI):** a host bring-up stages each `host_images` entry
  idempotently; a System provisions + boots each non-bare image to multi-user; the bare
  image meets the Option-A contract; two hosts stage different sets. The scratch/bare path
  and ppc64le remain unvalidated (no host), caveated in the README like the existing
  ppc64le note.
