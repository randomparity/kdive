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

- **Authoritative network config in a `cloud.cfg.d` drop-in, not only the seed.**
  `/etc/cloud/cloud.cfg.d/99-kdive.cfg` carries the netplan-v2 DHCP config (`match: {name:
  "e*"}`, interface-name-independent under the SLIRP NIC — the property the rhel NM keyfile
  provided, now uniform). cloud-init's *system config* network setting outranks the datasource,
  so putting network here — not only in a seed `network-config` file — defeats a base image that
  ships `network: {config: disabled}` (some vendor cloud images do), which would otherwise
  silently void the seed and reproduce the no-IP failure. The build also **strips any base
  drop-in that disables cloud-init network config** and asserts its absence (build self-check).

- **Pin the datasource + protect root.** The same drop-in sets `datasource_list: [NoCloud]`
  (no off-cloud metadata probing — the original hang) and `disable_root: false`. The latter is
  **defensive, not the load-bearing fix**: cloud-init's `disable_root` prefix only rewrites root
  keys it installs *from the datasource*, and this design provides none (the managed key is
  `--ssh-inject`'d onto the filesystem), so `disable_root: true` is most likely inert for us.
  The real root-SSH vectors a re-enabled cloud-init can touch (`PermitRootLogin`, `users_groups`
  root lock, `ssh_pwauth`) are verified by the live proof, not assumed. `growpart`/`resizefs`
  are disabled via the targeted `growpart: {mode: "off"}` + `resize_rootfs: false` keys (the
  `"off"` quoted so YAML does not read it as boolean false) — not a
  module-list override, which could drop a module the design needs (e.g. `ssh`) — quieting
  no-op boot noise on the partitionless whole-disk-ext4 layout (ADR-0030).

- **cloud-init owns network + SSH host keys.** The `ssh` module generates host keys (replacing
  the debian `kdive-sshd-keygen` oneshot and the distro `sshd-keygen@` reliance — the live proof
  asserts host keys exist and sshd answers on Debian, catching an ordering regression); the
  drop-in brings up the NIC.

- **The managed authorized key stays on `--ssh-inject`.** It is a libguestfs builtin, not
  drift-prone glue, and baking it into the filesystem guarantees the worker can log in even if
  cloud-init fails — the robustness `authorize_ssh_key` requires. cloud-init is scoped to the
  genuinely first-boot, interface-dependent concerns.

- **Leave cloud-init unit enablement to the vendor base and package preset — do not enumerate
  units by name.** The vendor cloud bases ship the cloud-init units enabled; the build only undoes
  the prior disable (`rm -f /etc/cloud/cloud-init.disabled`, strip any network-disabling drop-in),
  and on the lone virt-builder base (`fedora-kdive-ready-43`, ships no cloud-init) `--install
  cloud-init` enables them via the package systemd preset. Enumerating unit names is fragile:
  cloud-init 24.x renamed `cloud-init.service` to `cloud-init-network.service`, so `systemctl
  enable cloud-init.service` aborts the build on Debian 13 (live-found 2026-07-01) — naming the
  units couples the build to a cloud-init version.

- **Order `kdive-ready` after `network-online.target`.** The serial `ready` marker must imply the
  NIC obtained its cloud-init DHCP lease: without the edge the marker fires the same second the
  network comes up, so an `authorize_ssh_key` at `ready` races the lease and fails
  `transport_failure` (live-found on Debian 13, where `cloud-init.target` and the marker landed in
  the same second). local-libvirt renders exactly one NIC under SLIRP, which always leases, so
  `wait-online` returns immediately and cannot stall the provision (the multi-link stall the
  earlier draft feared does not arise on this topology).

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
- CI cannot boot the image (KVM + a multi-minute 6 GB rebuild are behind live-VM markers) and
  the unit tests assert only the argv shape, so a silent no-op (a base re-disabling network, a
  dropped seed) could emit correct-looking argv and still ship broken. `build()` therefore runs
  an **offline guestfish self-check** on the built qcow2 before publish — the `99-kdive.cfg`
  drop-in and NoCloud seed exist, cloud-init is installed and not re-disabled
  (`/etc/cloud/cloud-init.disabled` absent), and no `cloud.cfg.d` drop-in disables cloud-init
  networking — failing the build (`PROVISIONING_FAILURE`) on any host if not. The check does not
  assert unit-enable state (version-dependent unit names); enablement is the vendor base's and the
  package preset's responsibility. The end-to-end "SSH answers" proof remains operator-run behind
  the live-VM markers, not a CI gate.
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

- **Leave `kdive-ready` unordered w.r.t. the network** (rely on `cloud-init-local` pre-network NIC
  bring-up). This was the original draft; the live proof rejected it: on Debian 13 the marker and
  `cloud-init.target` landed in the same second, so `authorize_ssh_key` at `ready` raced the lease
  and failed. The `wait-online` stall the draft feared does not arise on local-libvirt's
  single-NIC SLIRP topology, so ordering after `network-online.target` is cheap here (accepted
  above). Efficacy verification still belongs to the future S2 `ssh_reachable` boot-probe.

- **Enumerate and enable the four cloud-init units by name** (`cloud-init-local`, `cloud-init`,
  `cloud-config`, `cloud-final`). Rejected: cloud-init 24.x renamed `cloud-init.service` to
  `cloud-init-network.service`, so `systemctl enable cloud-init.service` aborts the build on
  Debian 13 (live-found). The vendor base ships the units enabled and `--install` applies the
  package preset, so naming them adds only version fragility.
