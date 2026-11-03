# 0804 — Remote authority Systems have only the SSH-forward NIC

## Status

Accepted (2026-10-21)

## Context

An authority-owned remote-libvirt System
([ADR-0623](0623-authority-owned-system-provisioning.md)) runs on the authority's private
libvirt daemon. That daemon is an unprivileged session daemon owned by the authority account. Its
network driver has only its own network list, and nothing provisions a network into it.

The remote renderer gave every domain a libvirt `<interface type='network'>` naming the network
from the owner's manifest. On the authority daemon, `domain.create()` therefore failed with
"no network with matching name 'default'" (#3310). No authority System ever reached `ready`.

An unprivileged daemon cannot create a NAT or routed network. It can attach a guest to an
existing host bridge only through the setuid `qemu-bridge-helper` and a system-wide
`/etc/qemu/bridge.conf` allow list. That would put authority guests on the same layer-2 segment
as ordinary guests, and it depends on how each distribution packages the helper.

The guest's two forwards do not use the libvirt NIC. The gdbstub is a `-gdb tcp:` argument that
QEMU serves directly. SSH is the second, user-mode NIC from
[ADR-0291](0291-remote-ssh-bootstrap-injection.md): `-netdev user,restrict=on,hostfwd=...`. The
libvirt NIC only gave the guest its default route and its egress.

The local authority already runs on the same kind of daemon with no libvirt NIC. Its guests have
the single user-mode forward NIC, which is `restrict=on` unless the operator opts the resource
into egress ([ADR-0313](0313-local-libvirt-operator-gated-egress.md)).

## Decision

A remote authority System's domain carries no libvirt `<interface>`. Its only NIC is the
user-mode SSH-forward NIC, and the `-gdb` argument is unchanged.

- The remote authority manifest entry no longer has a `network` field. The manifest model and the
  controller-side preflight validator both reject one.
- The entry gains an operator-owned `guest_egress` boolean, default `false`, which extends
  ADR-0313's operator gate to the remote authority. `false` renders the forward NIC
  `restrict=on`, as before. `true` renders it `restrict=off`, so the NIC also carries
  guest-initiated NAT traffic.
- `guest_egress` is a field of the private provision intent. It is bound into the intent
  identity and checked against the manifest when the intent is adopted. Through the rendered
  QEMU arguments it is also part of the domain projection. If the operator flips the setting,
  an existing intent or domain is refused, not silently reused. No request field can set it.
- The v1 provision intent changes shape in place: `network` is replaced by `guest_egress`. An
  authority host must have no stored provision intents when this change is deployed.

The ordinary (non-authority) remote renderer output is unchanged.

## Consequences

- Authority Systems start on the private daemon without host network provisioning, a setuid
  helper or a system-wide bridge allow list. The manifest can no longer name an object that fails
  only at first start.
- With one NIC, the guest's default route is the user-mode gateway. The SSH reply returns through
  user-mode networking without the [ADR-0721](0721-remote-ssh-forward-return-path-is-a-guest-source-route.md)
  source route, so images without that contract also answer.
- With `guest_egress` off (the default), an authority guest has no outbound network. That
  includes in-guest object-store fetches.
- Residual risk with `guest_egress` on: user-mode networking maps its gateway address (`10.0.2.2`)
  to the host's loopback. The guest can therefore reach loopback-only services on the authority
  host as the authority account. That is tracked in #3311 and not addressed here.
- An operator manifest that still carries `network` is rejected at preflight and must be edited.
  A stored v1 intent from before this change no longer parses, and the authority fails closed on
  it.

## Considered & rejected

- **A second, libvirt-managed user-mode NIC for egress, on another subnet.** It keeps egress
  always on, and it exposes host loopback on every System, not only on operator-opted ones. It
  also adds a second user-mode NIC next to the forward NIC.
- **A session network in bridge-forward mode onto a host bridge, provisioned by the role and
  checked at preflight.** It keeps the manifest field and the renderer unchanged. But it puts
  authority guests on the same layer-2 segment as ordinary guests, and it depends on the setuid
  bridge helper and a system-wide allow list whose packaging differs by distribution.
- **Keeping `network` as an ignored legacy intent field.** It would let stored intents from
  before this change still tear down. No authority host holds one that needs it, so it is code
  without a consumer.
