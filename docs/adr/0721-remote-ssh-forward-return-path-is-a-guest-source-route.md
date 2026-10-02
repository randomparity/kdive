# 0721 — The remote SSH forward's return path is a guest source route

## Status

Accepted (2026-10-02)

## Context

[ADR-0291](0291-remote-ssh-bootstrap-injection.md) gives a remote-libvirt System an SSH endpoint
through a second NIC: `-netdev user,restrict=on,hostfwd=tcp:<ssh_addr>:<port>-:22`. The guest's
default route stays on the libvirt-network NIC.

The live run in #2810 found that the forward never answers (#3090). libslirp rewrites a forwarded
connection's source address to its gateway (`10.0.2.2`) only when the client address is loopback
or unspecified. Local-libvirt binds `127.0.0.1` and has a single NIC, so its replies always return
through slirp. Remote binds a routable `ssh_addr`. The SYN reaches the guest's slirp lease
(`10.0.2.15`) with the worker's real address as its source. The SYN-ACK follows the main table's
default route out of the libvirt NIC, and the connection stays in `SYN_SENT` in slirp.

## Decision

The remote base images carry a NetworkManager dispatcher script,
`/etc/NetworkManager/dispatcher.d/50-kdive-ssh-return-route`. The `guest_base_image` role
installs it from `deploy/remote-libvirt-guest-helpers/kdive-ssh-return-route`, the same way it
installs `fadump-capture.service`.

The script's contract:

- On `up` or `dhcp4-change`, when the DHCP lease is inside slirp's fixed `10.0.2.0/24`, it routes
  traffic sourced from the lease through slirp's gateway: `ip rule pref 2291 from <lease> lookup
  2291` and `ip route replace default via 10.0.2.2 dev <interface> table 2291`. A rule for an
  older lease is removed first.
- On `down`, it removes the table's route and rules when the table routes through that
  interface or is already empty.
- It keys on the lease subnet. It never matches an interface name or a PCI path.
- Table and rule priority 2291 belong to kdive inside the guest.

These stay unchanged: the main routing table, `rp_filter=1`, `restrict=on`, the domain XML, and
the authority projection. The kernel's strict reverse-path check passes, because the lookup for a
SYN to the lease uses the lease as source and resolves to the slirp NIC through the rule.

Images without NetworkManager (Ubuntu 24.04 and the bare image) do not get the script. Their return
path is #3091.

## Consequences

- Remote SSH parity needs the image contract. An image staged before this change must be rebuilt
  (`force_image_rebuild=true`), or the forward stays silent. The runbooks say so.
- The fix lives in the guest image, not in the provider. A custom image must ship an equivalent
  source route on its slirp lease or have no SSH parity.
- The match depends on slirp's default subnet and gateway. kdive renders the forward netdev with
  no `net=` option. A change that sets one must update the script in the same change.
- Guest-initiated traffic sourced from the lease now routes to slirp's gateway, where
  `restrict=on` drops it, as before.
- The fix survives a guest reboot and a kernel install, because NetworkManager runs the script on
  every activation.

## Considered & rejected

- **A host relay** (`socat` or `systemd-socket-proxyd` on the provider host bound to `ssh_addr`,
  feeding a loopback `hostfwd`). It makes slirp see a loopback client, but it adds a per-System
  host process that kdive must start, supervise, and reap over a connection that only speaks
  libvirt. The provider has no host-process channel.
- **Dropping the second NIC and using host DNAT** to the guest's libvirt address. It needs
  provider-host firewall writes per System and a live address lookup, and it breaks the "endpoint
  recorded in domain XML" invariant from ADR-0291.
- **The passt backend.** passt preserves the client's source address by design, so as a second
  NIC it has the same reply problem. Making it the only NIC replaces the libvirt-network
  interface that guest egress and the object-store fetches use.
- **Runtime injection over the guest agent** (writing the rule after boot). It does not survive
  a reboot or a lease change unless it is re-run on every boot. That is the job the dispatcher
  already does, and the agent hop would add a new shell exception beyond ADR-0291's.
- **An NM keyfile that matches the NIC by PCI path** (`addr=0x10`) with route rules. It couples
  the image to a QEMU slot number and to the PCI path naming, which differ across machine types.
  The lease subnet is the property slirp guarantees.
- **`rp_filter=2`.** Loose mode does not change where the reply is routed. On the diagnosis guest
  it did not help; only a route back through slirp's gateway did.
