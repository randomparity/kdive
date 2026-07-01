# Embrace cloud-init for rootfs first-boot — design (#962)

## Problem

kdive's local-libvirt rootfs build (`providers/local_libvirt/rootfs_build.py` +
`images/families/`) **masks/disables cloud-init** on every base image and hand-rolls per-family
first-boot config. That glue has drifted and broken: debian-family images ship
SSH-unreachable (live-proven 2026-07-01) because the build disables cloud-init but installs no
NIC network config — the guest boots with no IP, so sshd is up but unreachable
(`Connection timed out during banner exchange`), and `systems.authorize_ssh_key` fails
`transport_failure`. The rhel family only works because it installs an interface-independent
NetworkManager DHCP keyfile; the debian family installs nothing.

Every base image kdive uses ships cloud-init — the distro-maintained, uniform first-boot
mechanism. This design replaces the hand-rolled fragments with cloud-init, fed a deterministic
local datasource.

## Decision

Adopt **cloud-init as the uniform first-boot mechanism**, fed a **build-time baked NoCloud
seed** (`/var/lib/cloud/seed/nocloud/`). cloud-init then owns, across all families:

- **NIC DHCP** — an explicit netplan-v2 `network-config` matching all ethernet interfaces
  (`match: {name: "e*"}`, `dhcp4: true`), interface-name-independent under the SLIRP NIC.
- **SSH host-key generation** — cloud-init's `ssh` module, replacing the debian
  `kdive-sshd-keygen` oneshot and the distro `sshd-keygen@` reliance.

The build-time managed authorized key **stays on `--ssh-inject`** (a libguestfs builtin, not
drift-prone glue): it guarantees the worker's managed key is in the image independent of
cloud-init succeeding, which is the robustness the `authorize_ssh_key` path needs. cloud-init is
scoped to the genuinely first-boot, interface-dependent concerns (network, host keys).

### What is added (per built image)

1. `/etc/cloud/cloud.cfg.d/99-kdive.cfg` — pins cloud-init to the local seed and protects root
   login:
   ```yaml
   datasource_list: [ NoCloud ]
   disable_root: false
   ```
   - `datasource_list: [NoCloud]` stops cloud-init probing EC2/Azure/GCE metadata endpoints
     (network timeouts / the original "cloud-init hangs off-cloud" reason it was disabled).
   - `disable_root: false` is **mandatory**: the base images' default cloud config sets
     `disable_root: true`, which would prepend a `no-port-forwarding…` command to root's
     `authorized_keys` and break the managed-key root SSH the worker and `authorize_ssh_key`
     depend on. Re-enabling cloud-init without this would regress SSH.
   - Disable the disk-growth modules (`growpart`, `resizefs`) here too: the rootfs is a
     no-partition-table whole-disk ext4 (ADR-0030); growpart finds no partition and only adds
     boot noise.

2. `/var/lib/cloud/seed/nocloud/meta-data`:
   ```yaml
   instance-id: kdive-rootfs
   local-hostname: kdive
   ```
   Static (same per System) — acceptable: the managed key is build-time static, and per-System
   agent keys arrive via the runtime `authorize_ssh_key` append, not cloud-init.

3. `/var/lib/cloud/seed/nocloud/network-config` (netplan v2):
   ```yaml
   version: 2
   ethernets:
     kdive-dhcp:
       match:
         name: "e*"
       dhcp4: true
       dhcp-identifier: mac
   ```

4. `/var/lib/cloud/seed/nocloud/user-data` — minimal `#cloud-config` (host keys come from the
   default `ssh` module; nothing else needed here).

5. Ensure cloud-init is **enabled** (unmask; `systemctl enable cloud-init.service` where the
   base didn't). On the lone **virt-builder** base (`fedora-kdive-ready-43`, `is_cloud_image =
   False`), which does not ship cloud-init, **`--install cloud-init`** so the mechanism is
   uniform; otherwise removing the NM keyfile would leave it network-less.

### What is removed

- rhel: `_CLOUD_INIT_MASK` (stop masking) and the NetworkManager SSH-NIC keyfile
  (`_ssh_nic_keyfile_args` / `SSH_NIC_KEYFILE_*`).
- debian: the `/etc/cloud/cloud-init.disabled` touch, and the `kdive-sshd-keygen` unit + its
  enable.

### What stays unchanged (not cloud-init's job)

- `kdive-ready` serial readiness unit, kdump sysctl/`final_action`/`USE_KDUMP`, the `kdive-drgn`
  helper, the rhel SELinux permissive relabel, and the `machine-id` seed (closes the first-boot
  `preset-all`→kdump-disable landmine independent of cloud-init; now seeded on **every** built
  image, cloud and virt-builder, since re-enabling cloud-init makes an uninitialized machine-id
  more consequential).

### Readiness implies reachable

Extend the shared `kdive-ready` unit ordering with `Wants=network-online.target` +
`After=network-online.target` so `ready` implies the NIC obtained a DHCP lease (cloud-init's
renderer pulls in `systemd-networkd-wait-online`/`NetworkManager-wait-online`). This closes the
race where `authorize_ssh_key` runs at `ready` before cloud-init finished bringing up the
network. (Full SSH-answered verification remains the S2 `ssh_reachable` probe's job; this only
tightens `ready` to "network up".)

## Components touched

- `images/families/_fedora_customize.py` — add seed/`99-kdive.cfg` staging helpers (family-
  neutral, mirroring the existing tempfile+`--upload`+cleanup idiom); remove
  `SSH_NIC_KEYFILE_*` + `_ssh_nic_keyfile_args`; extend `readiness_unit()` ordering.
- `images/families/rhel.py` — drop the NM keyfile call and `_CLOUD_INIT_MASK`; add the seed
  fragment; keep SELinux/kdump/drgn/`--ssh-inject`.
- `images/families/debian.py` — drop `cloud-init.disabled` + `kdive-sshd-keygen`; add the seed
  fragment; keep kdump/drgn/`--ssh-inject`.
- `providers/local_libvirt/rootfs_build.py` — `--install cloud-init` on the non-cloud base so
  the seed applies uniformly; seed machine-id on all images.
- Tests: unit tests over the argv fragments (seed files written, NM keyfile/sshd-keygen gone,
  `disable_root: false` present, cloud-init unmasked) + a live e2e proof.

## Testing

- **Unit** (no libguestfs): assert each family's `customize_argv` writes the three seed files +
  `99-kdive.cfg` (with `datasource_list: [NoCloud]` and `disable_root: false`), no longer emits
  the NM keyfile / `cloud-init.disabled` / `kdive-sshd-keygen` / `_CLOUD_INIT_MASK`, and that
  `readiness_unit()` orders after `network-online.target`.
- **Anti-regression**: the capability tags (ADR-0287) and kdump/SELinux fragments are
  unchanged; the `--ssh-inject` managed key is still emitted.
- **Live e2e (the acceptance proof)**: rebuild debian-13 (and one rhel image), provision a
  System, `systems.authorize_ssh_key` succeeds, and an agent SSHes in and runs an in-guest
  command — the exact flow that fails today. Gated behind the live-VM markers.

## Considered & rejected

- **osbuild / image-builder** — no Rocky/Alma targets (kdive ships Rocky 8/9/10), host/
  entitlement constraints (RHEL needs RHSM; cross-family builds impossible on a Fedora host),
  and no fidelity gain (composes from repos, not the vendor cloud image). Would fragment, not
  unify. (#961 tracks the broader survey.)
- **mkosi** — a uniform, systemd-native from-packages builder that would delete cloud-init
  entirely, but it loses the vendor-cloud-image fidelity wanted for the RHEL family and is a
  build-plane rewrite. Parked as a future minimal-lane option (#961).
- **Per-provision NoCloud seed ISO** — buys per-System identity (instance-id, hostname,
  provision-time key injection) kdive does not need, at the cost of a second disk in the domain
  XML and teardown reclaim. Deferred; documented as the ADR alternative.
- **Move the managed key into cloud-init user-data** — cleaner "single authority" but less
  robust: a cloud-init failure would leave the worker unable to log in. Keeping `--ssh-inject`
  makes the managed key present independent of cloud-init.
