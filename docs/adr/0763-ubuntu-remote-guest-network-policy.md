# 0763 — Ubuntu remote guest networking uses native lease events

## Status

Accepted (2026-10-08)

## Context

Issue #3091 owns Ubuntu remote SSH parity and the bare image's support boundary.
The canonical Ubuntu 24.04 image has systemd-networkd, netplan and
networkd-dispatcher, but no network configuration. A real guest had its primary
interface down, no IPv4 address or route, and could not fetch an uploaded kernel.
[ADR-0721](0721-remote-ssh-forward-return-path-is-a-guest-source-route.md) separately
requires replies sourced from the slirp lease to use table and priority 2291.

Netplan's static interface policy cannot select a NIC by a lease not yet acquired.
Putting the slirp gateway on every Ethernet interface would select the wrong NIC.
Networkd-dispatcher already supplies interface/address/state events, including
startup replay. Native systemd enters configuring for newly acquired addresses or
routes and later configured when they are ready. No new network manager is needed.

The original scope excluded main-route redesign. The operator explicitly approved
one bounded exception: removing only the DHCP default through the
identified restricted slirp NIC. Without that exception, enabling DHCP on both NICs
can let the restricted NIC win the main default-route race. A source rule alone
cannot give ordinary, not-yet-source-bound outbound connections the correct route.
The operator approved this native policy and its bounded exception after design review.

## Decision

This decision amends ADR-0721 only for Ubuntu native lease events and the bounded
DHCP-default suppression described below. Its accepted NetworkManager policy and
existing helper behavior remain unchanged.

For the canonical Ubuntu cloud image only:

1. Declare netplan.io, networkd-dispatcher and iproute2 in its existing package list.
   They are already present in the inspected image; this makes prerequisites explicit.
2. Install an owned netplan policy enabling DHCPv4 on virtio Ethernet interfaces,
   without naming a NIC, MAC address or PCI slot. Retain systemd-networkd as renderer.
   Disable only cloud-init's network configuration to prevent competing ownership.
   Refuse unrelated preexisting netplan YAML instead of deleting or overwriting it;
   repeat builds accept the role's own file. Do not disable cloud-init generally.
3. Reuse the existing `kdive-ssh-return-route` implementation unchanged, installed as
   a root-owned executable under `/usr/local/libexec` on Ubuntu. NetworkManager
   images retain their current dispatcher installation and behavior.
4. Add a small networkd-dispatcher adapter. On configured/routable events, select
   the observed IPv4 lease in slirp's fixed subnet and pass it and the event's
   interface to the existing helper. A non-slirp event cannot change another NIC's
   owned route. On off/no-carrier/failed/linger, delegate the existing guarded
   teardown. A configured interface which no longer has a slirp lease also tears
   down its own old policy. Do not infer identity from interface names or slots.
5. Before installing the private return route for a verified slirp lease, remove
   only an existing main-table DHCP default via the slirp gateway on that same
   interface. Preserve static routes, defaults via other gateways/interfaces,
   connected routes, DNS and every unrelated routing table. Repeated events are
   idempotent. A failed query or mutation fails the hook; do not hide errors.
   This is the explicitly approved narrow exclusion amendment.
6. Install hook directories/targets root-owned 0755 as the native dispatcher
   requires. Enable networkd and its dispatcher. An owned service drop-in retains
   the distribution's optional arguments and adds `--run-startup-triggers` to its
   existing executable; no replacement daemon or poller. Startup replay handles
   service ordering and restart after the lease already exists.

DHCP renewals which reintroduce the default or replace an address cause native
configuration events; the adapter reapplies the same bounded policy. Losing a link
removes its private route/rule without touching another NIC's policy. No claim of
transactional multi-command routing or immunity to a privileged peer is added.

The bare catalog entry remains available for its existing purpose, explicitly
without remote SSH parity: it installs neither sshd nor a network policy. State this
in the catalog entry and operator docs; do not add an unused capability key, remove
the image, or claim bare SSH/reboot proof. Its existing unvalidated boot status
remains. Operators needing remote SSH must select a supporting image; this record
does not change per-provider runtime capabilities or add per-image admission APIs.

## Consequences

Rebuild Ubuntu images staged before this policy. Existing Systems and their disks
are not repaired in place. The role owns only its new image files; unrelated image
policy is a build error with a clean-image/rebuild remedy. No database migration.

Actual proof must show authenticated forwarded SSH before and after reboot, a new
boot ID, primary-interface ordinary egress, and the slirp source lookup through
2291. Repeat activation/lease renewal and dispatcher restart must preserve that
result. Record candidate roles and image digest; x86 does not prove native POWER.
Kernel-helper installation remains #3081/#3082 and is not required to prove the
stock Ubuntu kernel's SSH/reboot path here.

## Considered & rejected

- Static netplan routes on all NICs: the slirp gateway belongs to only one lease.
- NIC-name/PCI matching: contradicts ADR-0721's lease identity and machine portability.
- Replace networkd with NetworkManager: changes more ownership than an existing
  native dispatcher adapter, although the shared routing helper could be reused.
- Runtime QGA injection or a timer: adds a second policy owner and misses native
  lease lifecycle; it is not image provisioning.
- Broader route replacement: unnecessary; only the restricted slirp DHCP default
  conflicts with ordinary egress.

## References

- [Netplan DHCP and source-routing examples](https://netplan.readthedocs.io/en/stable/examples/).
- [systemd 255 DHCP state transitions](https://github.com/systemd/systemd/blob/v255/src/network/networkd-dhcp4.c).
- Ubuntu image's installed networkd-dispatcher 2.2.4 and systemd 255 sources were
  inspected alongside its package manifest; live proof remains mandatory.
