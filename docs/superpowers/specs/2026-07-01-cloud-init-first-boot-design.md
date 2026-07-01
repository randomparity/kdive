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
seed** (`/var/lib/cloud/seed/nocloud/`) plus an authoritative kdive `cloud.cfg.d` drop-in.
cloud-init then owns, across all families:

- **NIC DHCP** — an explicit netplan-v2 network config matching all ethernet interfaces
  (`match: {name: "e*"}`, `dhcp4: true`), interface-name-independent under the SLIRP NIC.
- **SSH host-key generation** — cloud-init's `ssh` module, replacing the debian
  `kdive-sshd-keygen` oneshot and the distro `sshd-keygen@` reliance.

The build-time managed authorized key **stays on `--ssh-inject`** (a libguestfs builtin, not
drift-prone glue): it guarantees the worker's managed key is in the image independent of
cloud-init succeeding, which is the robustness the `authorize_ssh_key` path needs. cloud-init is
scoped to the genuinely first-boot, interface-dependent concerns (network, host keys).

### What is added (per built image)

1. **`/etc/cloud/cloud.cfg.d/99-kdive.cfg`** — the authoritative kdive first-boot config. It
   carries the network config directly (not only in the seed) **on purpose**: cloud-init's
   *system config* network setting outranks the datasource, so a base image that ships
   `network: {config: disabled}` (some vendor cloud images do, to hand networking to the
   platform) would otherwise silently void a seed `network-config` and reproduce today's
   no-IP failure. Naming it `99-kdive.cfg` sorts it after typical vendor drop-ins (later
   drop-in wins on merge); the build additionally **strips any base drop-in that disables
   cloud-init network config** (see build self-check below) so precedence cannot be lost.

   ```yaml
   datasource_list: [ NoCloud ]        # no off-cloud metadata probing (the original hang)
   disable_root: false                 # defensive; see note
   network:
     version: 2
     ethernets:
       kdive-dhcp:
         match: { name: "e*" }
         dhcp4: true
         dhcp-identifier: mac          # per-System DUID independent of the shared machine-id
   # Disable disk-growth with targeted config keys — NOT by overriding the module lists.
   # The rootfs is a no-partition-table whole-disk ext4 (ADR-0030), so growpart finds no
   # partition and only adds boot noise. `growpart: {mode: off}` + `resize_rootfs: false`
   # disable the behavior without rewriting `cloud_config_modules`, which would risk dropping
   # a module the design needs (e.g. `ssh`, which generates host keys).
   growpart: { mode: off }
   resize_rootfs: false
   ```

   **`disable_root: false` is defensive, not the load-bearing fix.** cloud-init's `disable_root`
   prefix only rewrites root keys that cloud-init installs *from the datasource*; this design
   provides no datasource keys (the managed key is `--ssh-inject`'d onto the filesystem), so
   `disable_root: true` would most likely be inert for us. It is set `false` as cheap insurance.
   The *actual* root-SSH regression vectors a re-enabled cloud-init can introduce —
   `PermitRootLogin` left by the vendor sshd config, the `users_groups` module locking the root
   account, `ssh_pwauth` — are **verified by the live proof** (see Testing), not assumed away.

2. **`/var/lib/cloud/seed/nocloud/meta-data`**:
   ```yaml
   instance-id: kdive-rootfs
   local-hostname: kdive
   ```
   Static (same per System) — acceptable: the managed key is build-time static, and per-System
   agent keys arrive via the runtime `authorize_ssh_key` append, not cloud-init.

3. **`/var/lib/cloud/seed/nocloud/user-data`** — minimal `#cloud-config`. Host keys come from
   the default `ssh` module; network comes from the drop-in above. (This file exists so NoCloud
   has a complete seed; it carries no key or network config.)

4. **Enable the full cloud-init pipeline — all four units, not just one.** cloud-init is a
   four-unit pipeline plus a generator: `cloud-init-local.service` applies the datasource
   network config at the **pre-network** stage, then `cloud-init.service`,
   `cloud-config.service`, `cloud-final.service`. Enabling/unmasking only `cloud-init.service`
   leaves `cloud-init-local` off and the NIC unconfigured — the exact bug this fixes. So:
   - **cloud images (masked today):** unmask **all four** (`cloud-init-local`, `cloud-init`,
     `cloud-config`, `cloud-final`) — the rhel `_CLOUD_INIT_MASK` masks all four — and confirm
     they are enabled.
   - **virt-builder base** (`fedora-kdive-ready-43`, `is_cloud_image = False`, ships no
     cloud-init): `--install cloud-init`, then **explicitly `systemctl enable` all four** — a
     `--install` in the offline virt-customize chroot may not apply the package's systemd
     preset, so do not rely on the preset.

### What is removed

- rhel: `_CLOUD_INIT_MASK` (stop masking) and the NetworkManager SSH-NIC keyfile
  (`_ssh_nic_keyfile_args` / `SSH_NIC_KEYFILE_*`).
- debian: the `/etc/cloud/cloud-init.disabled` touch, and the `kdive-sshd-keygen` unit + its
  enable. **Dependency:** with `kdive-sshd-keygen` gone, cloud-init's `ssh` module must generate
  host keys **before `ssh.service` starts on Debian** (cloud-init ships `Before=sshd.service`;
  Debian's unit is `ssh.service`). The live proof asserts host keys exist and sshd answers, so a
  broken ordering (the #824 keyless-sshd failure) would fail the acceptance test rather than
  ship silently.

### What stays unchanged (not cloud-init's job)

- `kdive-ready` serial readiness unit (ordering **unchanged** — see below), kdump
  sysctl/`final_action`/`USE_KDUMP`, the `kdive-drgn` helper, the rhel SELinux permissive
  relabel, and the `machine-id` seed (closes the first-boot `preset-all`→kdump-disable landmine
  independent of cloud-init; now seeded on **every** built image, cloud and virt-builder, since
  re-enabling cloud-init makes an uninitialized machine-id more consequential).

### Readiness ordering is intentionally NOT changed

An earlier draft ordered `kdive-ready` `After=network-online.target`. Dropped: it adds a
`systemd-*-wait-online` timeout (~120 s) onto the critical provisioning path — wait-online
blocks on *all* managed links by default, so any un-leased NIC would stall or delay reaching
`ready`. It is also unnecessary here: `cloud-init-local` brings the NIC up at the pre-network
stage, long before `kdive-ready` fires at `multi-user.target`, so there is no real race for
`authorize_ssh_key` to lose. Verifying that SSH actually answers at `ready` is the S2
`ssh_reachable` boot-probe's job (a future slice), which can gate on reachability without
importing a wait-online stall into every provision.

## Components touched

- `images/families/_fedora_customize.py` — add seed + `99-kdive.cfg` staging helpers (family-
  neutral, mirroring the existing tempfile + `--upload` + cleanup idiom); remove
  `SSH_NIC_KEYFILE_*` + `_ssh_nic_keyfile_args`. `readiness_unit()` is **not** changed.
- `images/families/rhel.py` — drop the NM keyfile call and `_CLOUD_INIT_MASK`; unmask/enable the
  four cloud-init units; add the seed + drop-in; keep SELinux/kdump/drgn/`--ssh-inject`.
- `images/families/debian.py` — drop `cloud-init.disabled` + `kdive-sshd-keygen`; enable the
  four cloud-init units; add the seed + drop-in; keep kdump/drgn/`--ssh-inject`.
- `providers/local_libvirt/rootfs_build.py` — `--install cloud-init` + enable the four units on
  the non-cloud base; seed machine-id on all images; add the **built-image self-check** below.

### Build-time self-check (closes the CI blind spot)

CI cannot run the live boot (KVM + a ~minutes 6 GB rebuild are behind live-VM markers), and the
unit tests only assert the **argv shape** — so a silent no-op (a base that re-disables network,
or a missed unit-enable) would emit correct-looking argv and still ship broken. To catch that
without booting, `build()` runs a fast **offline guestfish assertion** on the freshly built
qcow2 before publishing: (a) no remaining `/etc/cloud/cloud.cfg.d/*` drop-in sets
`network: {config: disabled}`; (b) the four cloud-init units are enabled — asserted by
`is-enabled` status / the presence of their enable symlinks in the correct wants-dir
(cloud-init's units are `WantedBy=cloud-init.target`, so the symlinks live in
`cloud-init.target.wants/`, and `cloud-init.target` itself is enabled into
`multi-user.target.wants/`; the exact paths are confirmed against a real image in the plan);
(c) `/etc/cloud/cloud.cfg.d/99-kdive.cfg` and the NoCloud seed files exist. A failed assertion fails the build (`PROVISIONING_FAILURE`),
so a regression is caught at build time on any host, not only on the live-VM machine.

## Testing

- **Unit** (no libguestfs): assert each family's `customize_argv` writes the seed + drop-in with
  `datasource_list: [NoCloud]`, `disable_root: false`, and the `network:` DHCP block; enables all
  **four** cloud-init units (not just `cloud-init.service`); and no longer emits the NM keyfile /
  `cloud-init.disabled` / `kdive-sshd-keygen` / `_CLOUD_INIT_MASK`. `readiness_unit()` output is
  unchanged (regression pin).
- **Anti-regression**: the capability tags (ADR-0287) and kdump/SELinux fragments are unchanged;
  the `--ssh-inject` managed key is still emitted.
- **Build self-check** (unit-testable via the injected guestfs seam): the offline assertion
  passes on a correctly-built tree and fails when a `network: {config: disabled}` drop-in or a
  missing cloud-init unit-enable is injected.
- **Live e2e — operator-run, not a CI gate.** CI green proves argv + offline structure, **not**
  that cloud-init actually DHCPs the NIC. The acceptance proof is operator-run behind the
  live-VM markers: rebuild debian-13 (and one rhel image), provision a System,
  `systems.authorize_ssh_key` succeeds, and an agent SSHes in as root and runs an in-guest
  command — asserting an IP is present, host keys exist, and root key-login works (covering the
  `PermitRootLogin` / `users_groups` / `ssh_pwauth` vectors) — the exact flow that fails today.

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
- **Order `kdive-ready` after `network-online.target`** — imports a wait-online timeout onto the
  provisioning path for no real race (cloud-init-local configures the NIC pre-network);
  reachability verification belongs to the S2 `ssh_reachable` probe.
