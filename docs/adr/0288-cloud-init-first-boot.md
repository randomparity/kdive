# ADR 0288 — Embrace cloud-init for rootfs first-boot via a baked NoCloud seed

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers

## Context

The local-libvirt rootfs build masks/disables cloud-init on every base image and hand-rolls
per-family first-boot configuration: the rhel family installs a NetworkManager DHCP keyfile
(`SSH_NIC_KEYFILE_*`, `_fedora_customize.py`) and masks cloud-init; the debian family touches
`/etc/cloud/cloud-init.disabled` and stages a `kdive-sshd-keygen` oneshot. This glue drifted and
broke: debian-family images ship **SSH-unreachable** (live-proven 2026-07-01) — the build
disables cloud-init but installs no NIC network config, so the guest boots with no IP, sshd is
up but never answers (`Connection timed out during banner exchange`), and
`systems.authorize_ssh_key` fails `transport_failure` (exit 255). The debian family's own comment
assumed "cloud-init's cloud-ifupdown-helper DHCPs the NIC" — cloud-init that the same build
disables.

cloud-init was originally disabled for concrete reasons: off-cloud there is no datasource, so
cloud-init either does nothing useful or hangs probing metadata endpoints; and an uninitialized
`/etc/machine-id` triggered a first-boot `systemctl preset-all` that disabled `kdump.service`
(proven `kexec_crash_loaded=0`). But every base image kdive uses (Fedora Cloud, Rocky/CentOS
GenericCloud, Debian genericcloud) **ships cloud-init**, and both original problems have targeted
fixes: a local NoCloud seed removes the datasource hang, and seeding `machine-id` closes the
`preset-all` landmine. See `docs/superpowers/specs/2026-07-01-cloud-init-first-boot-design.md`.

## Decision

Make **cloud-init the uniform first-boot mechanism**, fed a **build-time baked NoCloud seed**,
and delete the hand-rolled first-boot fragments.

- **Seed (build-time, static).** Write `/var/lib/cloud/seed/nocloud/{meta-data,network-config,
  user-data}` into the image during `virt-customize`. `network-config` is netplan-v2 DHCP
  matching all ethernet interfaces (`match: {name: "e*"}`), interface-name-independent under the
  SLIRP NIC — the property the rhel NM keyfile provided, now uniform. cloud-init finds the local
  seed instantly; no datasource probe, no hang.

- **Pin + protect via `/etc/cloud/cloud.cfg.d/99-kdive.cfg`.** `datasource_list: [NoCloud]`
  (no off-cloud metadata probing) and **`disable_root: false`** — mandatory, because the base
  images default `disable_root: true`, which would clobber root's `authorized_keys` and break
  the managed-key root SSH the worker and `authorize_ssh_key` depend on. Disable `growpart`/
  `resizefs` (no-op noise on the partitionless whole-disk-ext4 layout, ADR-0030).

- **cloud-init owns network + SSH host keys.** The `ssh` module generates host keys (replacing
  the debian `kdive-sshd-keygen` oneshot and the distro `sshd-keygen@` reliance); the seed's
  `network-config` brings up the NIC.

- **The managed authorized key stays on `--ssh-inject`.** It is a libguestfs builtin, not
  drift-prone glue, and baking it into the filesystem guarantees the worker can log in even if
  cloud-init fails — the robustness `authorize_ssh_key` requires. cloud-init is scoped to the
  genuinely first-boot, interface-dependent concerns.

- **Uniform presence.** cloud-init is unmasked/enabled on the cloud bases; on the lone
  virt-builder base (`fedora-kdive-ready-43`, which does not ship cloud-init) it is installed
  (`--install cloud-init`) so removing the NM keyfile does not leave it network-less.

- **`ready` implies network.** The shared `kdive-ready` unit gains `Wants=/After=
  network-online.target`, so `ready` implies a DHCP lease and `authorize_ssh_key` at `ready`
  does not race cloud-init's network bring-up.

- **Keep the kdive-specific pieces:** `kdive-ready`, kdump sysctl/`final_action`/`USE_KDUMP`,
  `kdive-drgn`, the rhel SELinux permissive relabel, and the `machine-id` seed (now on every
  built image).

## Consequences

- Debian (and any future genericcloud) images boot with a DHCP NIC and answer SSH; the
  live-proven breakage is fixed at the root, uniformly.
- Per-family first-boot glue shrinks: the NM SSH-NIC keyfile, `kdive-sshd-keygen`, the
  cloud-init masking, and `/etc/cloud/cloud-init.disabled` are removed. Network + host keys are
  the distro's maintained code, not ours.
- Re-enabling cloud-init is guarded against its known hazards: datasource hang (pinned to
  NoCloud), root-login clobber (`disable_root: false`), and the `preset-all`→kdump landmine
  (`machine-id` seed retained).
- No migration, no domain-XML change, no provisioning change: the seed lives entirely in the
  build plane. `build-fs` output changes (seed files present, NM keyfile/sshd-keygen absent);
  provision/boot/teardown are untouched.
- Capability tags (ADR-0287) are unchanged: `ssh` remains build-truthful, and now runtime-
  effective. The S2 `ssh_reachable` boot-probe (future) verifies efficacy end-to-end.

## Considered & rejected

- **osbuild / image-builder.** The native Fedora/RHEL builder, but it targets no Rocky/Alma
  (kdive ships Rocky 8/9/10), cannot build cross-family on a Fedora host (RHEL needs an RHSM
  entitlement; a Fedora host cannot build Debian at all), needs root/loopback/mount, and gives
  no fidelity gain (it composes from RPM repos rather than starting from the vendor cloud image).
  It would fragment the build rather than unify it. Broader survey tracked in #961.

- **mkosi.** A systemd-project, distro-uniform, from-packages builder that would remove
  cloud-init entirely and cover every kdive distro (incl. Rocky/Alma). Rejected for now because
  it builds minimal images from packages rather than the vendor cloud image, losing the
  RHEL/Fedora vendor-userland fidelity that matters when triaging whether an issue is even a
  kernel issue, and because it is a full build-plane rewrite. Parked as a future minimal-lane
  option (#961).

- **Per-provision NoCloud seed ISO (dynamic CIDATA).** Would give per-System cloud-init identity
  (instance-id, hostname, provision-time key injection). Rejected: kdive's managed key is
  build-time static and per-System agent keys arrive via the runtime `authorize_ssh_key` append,
  so the per-System identity buys nothing here, while the ISO adds a second disk to the domain
  XML and a teardown-reclaim path. The baked seed keeps the change inside the build plane.

- **Move the managed key into cloud-init user-data.** A single key authority, but a cloud-init
  failure would leave the worker unable to log in. `--ssh-inject` keeps the managed key present
  independent of cloud-init.

- **Minimal fix: add a static `systemd-networkd` DHCP `.network` to the debian family only.**
  Fixes the immediate bug but keeps the divergent, hand-rolled per-family first-boot model this
  ADR is replacing, and leaves the rhel NM keyfile and debian sshd-keygen glue in place.
