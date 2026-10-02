# Return path for the remote-libvirt SSH forward (#3090)

Decision record: [ADR-0721](../../adr/0721-remote-ssh-forward-return-path-is-a-guest-source-route.md),
with an amendment to [ADR-0291](../../adr/0291-remote-ssh-bootstrap-injection.md).

## Problem

A remote System provisioned with `ssh_addr`/`ssh_range` never answers SSH on its forward. The
domain has two NICs: the libvirt-network NIC, which holds the guest's default route, and the
user-mode (slirp) NIC that carries `hostfwd=tcp:<ssh_addr>:<port>-:22`. libslirp rewrites a
forwarded connection's source address to its gateway (`10.0.2.2`) only when the client address is
loopback or unspecified. Local-libvirt binds `127.0.0.1` and has one NIC, so it works. Remote binds
a routable `ssh_addr`, so the SYN reaches the guest's slirp NIC (`10.0.2.15`) with the worker's own
address. The guest's route back to the worker is the main table's default route on the libvirt NIC:
with strict `rp_filter=1` (Rocky 10) the guest drops the SYN, and with loose `rp_filter=2`
(Fedora) its SYN-ACK leaves through the libvirt NIC. The client never gets an answer. `systems.authorize_ssh_key` then fails `transport_failure`.

## Design

1. **Dispatcher script.** `deploy/remote-libvirt-guest-helpers/kdive-ssh-return-route` is a POSIX
   `sh` NetworkManager dispatcher script. It is called with `<interface> <action>` and the lease in
   `DHCP4_IP_ADDRESS`.
   - `up` or `dhcp4-change`, lease inside `10.0.2.0/24`: `ip -4 route replace default via
     10.0.2.2 dev <interface> table 2291`, then `ip -4 rule flush table 2291` (drops a rule for an
     old lease), then `ip -4 rule add pref 2291 from <lease> table 2291`.
   - `up` or `dhcp4-change`, lease missing or outside that subnet: no change. This is the libvirt
     NIC's own event.
   - `down`: when table 2291 has a route through `<interface>`, flush the table and the table's
     rules. When the table is already empty (the kernel dropped the route with the device), flush
     only the rules. When the table routes through another interface, no change.
   - Any other action: no change.
   - The script keys on the lease subnet, never on an interface name or PCI path. Slirp fixes the
     subnet and gateway (the forward's `-netdev user` carries no `net=` option).
2. **Image role.** `guest_base_image/tasks/build_one.yml` gains one `virt-customize` task beside the
   fadump unit. It uploads the staged copy to a temporary path and, when
   `/etc/NetworkManager/dispatcher.d` exists in the image, installs it as
   `/etc/NetworkManager/dispatcher.d/50-kdive-ssh-return-route` (root:root, 0755) and runs
   `restorecon` when present. An image without NetworkManager skips it. On the catalog that is
   the Ubuntu 24.04 and bare images, owned by #3091.
3. **Unchanged.** The main routing table, the image's `rp_filter` setting (1 on Rocky 10, 2 on
   Fedora), `restrict=on`, the domain XML, and the authority projection. Even strict
   `rp_filter=1` passes, because the reverse-path lookup for a SYN to
   the lease uses the lease as source, matches the rule, and resolves to the slirp NIC.
4. **Docs.** The `_append_ssh_forward` docstring stops claiming the forward mirrors local and states
   the return-path dependency. The guest-helpers README and both runbooks say the image carries the
   script and that an image staged before this change must be rebuilt
   (`force_image_rebuild=true`). The live SSH-parity test's module docstring names the return
   path as a prerequisite of the spine legs.

## Failure model

1. **Actors and deployments.** The operator builds images with the `guest_base_image` role on a
   remote provider host. NetworkManager runs the script as root in the guest. The worker and
   agents connect from the worker network to `ssh_addr:<port>`. The deployment is remote-libvirt
   with SSH parity enabled, on the Fedora 43 and Rocky 10 catalog images.
2. **Invariants.** Only traffic sourced from the slirp lease uses table 2291. Guest egress on the
   libvirt NIC is untouched. `restrict=on` still drops guest-initiated slirp traffic, including
   traffic now routed to slirp's gateway by the rule.
3. **Accepted failure classes.**
   - An image staged before this change has no script; SSH parity stays broken until the operator
     rebuilds it. The runbooks state this.
   - A guest whose NetworkManager does not manage the slirp NIC (custom images) gets no route. The
     contract is documented in ADR-0721.
   - A user inside the guest can remove the rule; the guest is the System owner's.
   - An operator who sets a different slirp `net=` would break the subnet match. The forward's
     netdev is rendered by kdive with no `net=`, so this needs a code change that ADR-0721 names.
4. **Covered elsewhere.** Ubuntu 24.04 and bare images: #3091. Host-key pinning: ADR-0291.
   ppc64le remote proof: #2803.

## Success

- AC1: script unit tests, with `ip` stubbed on `PATH`, cover an in-range `up`, an out-of-range
  `up`, a `down` that owns the table, a `down` for another interface, a `down` after the kernel
  dropped the route, and a `dhcp4-change`. Each test is seen to fail on a controlled fault.
- AC2: a contract test pins the role task: it uploads the staged script and installs it at the
  dispatcher path with root ownership and mode 0755, guarded on the dispatcher directory.
- AC3: live proof on lab hosts. Both images rebuilt with the script present. For each image, a new
  remote System passes `ssh_info`, `check_ssh_reachable`, `authorize_ssh_key`, and an agent SSH
  login; in the guest the rule is present, `rp_filter` is unchanged from the image default, and the forward is ESTAB. After a
  guest reboot SSH works again. Guest egress over the libvirt NIC works, and guest-initiated
  traffic out the slirp NIC is still dropped.
