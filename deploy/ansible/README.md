# Ansible: remote-libvirt host bring-up

Automates `docs/operating/runbooks/remote-libvirt-host-setup.md` steps 1–6 for a
kdive **remote-libvirt** provider host (Ubuntu 26.04 / Fedora 44 / RHEL-Rocky 10,
x86_64 or ppc64le). Design: `docs/archive/superpowers/specs/2026-06-18-ansible-remote-libvirt-host-setup-design.md`.

## What it produces

- A host serving a `qemu+tls` mutual-TLS listener on `:16514`, via the **per-distro**
  daemon model: Fedora/RHEL run the **modular** daemons (`libvirtd*` masked,
  `virtproxyd-tls.socket`); Ubuntu/Debian keep the **monolithic** `libvirtd`
  (`libvirtd-tls.socket`) — Ubuntu does not package the modular daemons.
- A `dir` storage pool + the `default` network.
- A firewalld/ufw ACL restricting `:16514` and the gdbstub range to `worker_cidr`,
  **enforced** on both distros (the gdbstub tier is raw TCP — the ACL is its only auth).
  Fedora/RHEL use firewalld; on Ubuntu the role allows SSH, sets
  `DEFAULT_FORWARD_POLICY=ACCEPT` (so enabling ufw doesn't break libvirt guest NAT
  egress), enables ufw (`gdbstub_acl_ufw_enable`, default true), and asserts it is
  active — failing closed rather than leaving the debug ports open. Set the var false
  only if you enforce by other means.
- The operator-staged images each host selects from a **catalog** (optional; `image.yml`),
  each carrying the in-guest helpers.
- A controller-side `systems.toml` block per host: one `[[image]]` per staged image plus
  the host's `[[remote_libvirt]]` naming a default `base_image`.

The default image is **lean** (provision / `host_dump` path). Set
`include_kernel_debuginfo: true` (and the crashkernel value) on a catalog entry for a
drgn-live/kdump image.

## Layout

- `site.yml` — full **remote-libvirt provider** host bring-up: stack → tls →
  pool/net → acl → emit facts.
- `playbooks/runner.yml` — a distinct path: the **local-libvirt self-hosted CI
  runner** (Ubuntu 26.04 LTS) for the native-KVM `live_vm` tier (epic #1289
  sub-issue B, #1291). It reuses `libvirt_stack` + `libvirt_pool_net` and adds
  `live_vm_host` (the `live_vm` environment-contract delta — toolchain, the
  system-3.14 venv, AppArmor-confined staging, short `XDG_RUNTIME_DIR`,
  host-contract gate) and `github_runner` (register a runner, installed stopped
  until the trusted-events posture is set). No TLS listener, no gdbstub ACL.
  Walkthrough:
  [`docs/operating/runbooks/self-hosted-kvm-runner.md`](../../docs/operating/runbooks/self-hosted-kvm-runner.md).
- `playbooks/pki.yml` — controller-side CA + per-host server certs + worker client
  bundle. Run once. The client bundle is written to `artifacts/client/` (gitignored,
  **vault the keys**); it is never fetched back from a host.
- `playbooks/image.yml` — the slow catalog-image build: loops over each host's
  `host_images`, building/staging each via its `source` (`virt-builder` / `cloud-image` /
  `scratch`). Opt-in.
- `inventory/` — `hosts.yml`, `group_vars/all.yml` (the tunable surface incl. the image
  catalog), per-host vars (incl. `host_images`).

## Usage

```bash
cd deploy/ansible
ansible-galaxy collection install -r requirements.yml

# 1. Generate PKI (controller-side, once). Vault the private keys.
ansible-playbook playbooks/pki.yml

# 2. Bring up the host(s).
ansible-playbook site.yml

# 3. Build + stage each host's selected catalog images (slow; opt-in).
ansible-playbook playbooks/image.yml
```

## Image catalog (`inventory/group_vars/all.yml` + `host_vars/`)

`kdive_image_catalog` is the list of buildable images; `kdive_image_defaults` holds the
shared fields (helper set, toggles, `arch_alias`, Fedora/RHEL package set). Each entry has
a logical `name`, `distro`/`version`, a `source` (`virt-builder` / `cloud-image` /
`scratch`), and optional per-entry overrides (`packages`, `helpers`, `arches`,
`host_distros`, `root_device`, `cloud_image_url`, `force`). The shipped catalog covers
**fedora** (virt-builder), **ubuntu** + **rocky** (cloud-image), and a **bare** image
(scratch).

Each host picks images by name in `host_vars/<host>.yml`:

```yaml
host_images: [fedora-kdive-remote-base-43, bare-kdive-remote-base]
host_default_image: fedora-kdive-remote-base-43   # optional; defaults to host_images[0]
```

`image.yml` stages exactly that host's `host_images` (idempotent per staged volume unless
`force_image_rebuild` or a per-entry `force`). The role fails fast if a `host_images` name is
absent from the catalog or its `arches` excludes the host arch.

### Build-host admission (`host_distros`)

Some entries can only be produced on some hosts. `bare-kdive-remote-base` is built with
`dnf --installroot`/`debootstrap` from the host's own repos and needs `busybox`, which the
RHEL/Rocky/Alma base repos do not carry — so it declares
`host_distros: [Fedora, Debian, Ubuntu]` (`ansible_distribution` values, not
`ansible_os_family`: Fedora and Rocky are both `RedHat` and differ on exactly this package).
An entry with no `host_distros` is buildable anywhere.

`image.yml` **skips** an entry this host cannot build, reports why, and builds the rest — one
undeliverable entry no longer costs the host its other images. A `host_default_image` the
host cannot build still fails fast, since that host would have no usable
`[[remote_libvirt]]` block. See
[ADR-0481](../../docs/adr/0481-build-host-image-admission-and-staged-volume-confirmation.md).

### External-boot guest userland

External-boot identity proof reads the running guest's kernel identity by spawning
`/usr/bin/uname` and `/usr/bin/cat` through the guest agent, **by absolute path**. Every catalog
image owes both as executable files, and `coreutils` is what supplies them on both package
families — an image exposing those applets only at `/usr/sbin/busybox` does not satisfy the check.
The scratch installroot names `coreutils` explicitly rather than relying on `systemd` to pull it
in; the other three entries take their userland from a base image this repository downloads rather
than composes, so they rest on the build check alone.

`guest_base_image` verifies both paths in each qcow2 it builds **before** staging it, so a
non-conformant image never reaches the pool and never appears in an `[[image]]` block. The check is
guarded like the rest of the build, so it does not run when a staged volume is already present: an
image staged earlier stays unverified until it is rebuilt with `force_image_rebuild=true`, the same
refresh the in-guest helpers need. See
[ADR-0590](../../docs/adr/0590-external-boot-requires-a-posix-userland-in-every-catalog-image.md).

### What the emitted `[[image]]` blocks claim

`remote_libvirt_facts` stats each selected image's volume in `storage_pool_target` and emits
an `[[image]]` block **only** for volumes that are actually there — `kind = "staged"` is a
claim about the host, not about the inventory. Selected images with no volume are listed as
`# OMITTED` in the artifact header.

The facts play runs from `site.yml` (usage step 2), which never builds images, so on a fresh
host the fragment starts out with no `[[image]]` blocks. **Re-run `site.yml` after
`playbooks/image.yml`** to regenerate a complete one. If `host_default_image` is among the
omitted, the fragment is marked `# INCOMPLETE`: pasting it in is rejected by the app at load
(`base_image` names an undeclared image) rather than registering an image that only fails
later when a System tries to boot it.

## Config surface (`inventory/group_vars/all.yml`)

`disable_security_driver` defaults **false** (opt-in; try the SELinux/AppArmor label
fix first). `kdive_image_defaults.include_kernel_debuginfo` defaults **false**.
`vcpus`/`memory_mb` are the required billable ceiling. `gdb_addr` defaults to an empty
string in `group_vars/all.yml` but is functionally required — both `playbooks/pki.yml`
(generate mode) and the `remote_libvirt_facts` role assert it is non-empty and fail fast
with a clear message if it is not set in `host_vars/`.

## Verification

- Idempotence: `ansible-playbook site.yml` twice → the second run reports **0 changed**.
- Off-CIDR ACL refusal: from a host **outside** `worker_cidr`, a TCP connect to `:16514`
  (and the gdbstub range) must be refused/timed out, while an in-CIDR worker connects.
  The role asserts enforcement in-play (firewalld drop-rules present on Fedora; ufw active
  on Ubuntu). Then `just check-remote-libvirt <host>` and the runbook step-8 worker→host
  TLS connect confirm the path end-to-end. (Verified on both distros 2026-06-19.)
- Only **one** host's `[[remote_libvirt]]` block may be loaded into a given
  `systems.toml` (the reconciler is singleton until multi-instance remote selection lands).

## Caveats

- The in-guest helpers are a **Fedora/RHEL-family** reference implementation
  (`grubby`/`dracut`/`grub2-reboot`/`kdump-utils`), so the full kdive
  build→install→boot→debug arc ships for **fedora** and **rocky**. The **ubuntu** image
  stages, boots, and connects its guest agent (provision / `host_dump` path), but
  `runs.install` (the `kdive-install-kernel` `grubby` path) needs a Debian helper variant
  — **unvalidated / tracked separately**.
- The **scratch/bare** path is implemented but **unvalidated** (no scratch-capable test
  host): it builds the rootfs from the host OS family but the bootloader install + boot
  must be confirmed on hardware, like the ppc64le note below. It is admitted only on
  Fedora/Debian-family hosts (`host_distros`, above), so that confirmation has to happen
  on one of those. Its userland is declared (the installroot names `busybox` and `coreutils`) and
  the build verifies the external-boot paths before staging, but neither the build nor that
  verification has run on a real host.
- ppc64le paths are implemented but **unvalidated** (no ppc64le test host).
- `root_device` on a catalog entry is metadata only for remote-libvirt — the in-guest GRUB
  owns the real root (ADR-0183); the platform injects no `root=` for remote.
- Molecule-in-Docker is intentionally not used: it cannot exercise KVM or `virt-builder`.
