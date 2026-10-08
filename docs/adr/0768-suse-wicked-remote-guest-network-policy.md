# 0768 — Leap remote guests retain native Wicked networking

## Status

Proposed

## Context

The checksum-pinned Leap15.6 image for #3082 contains Wicked0.6.77, netconfig0.85.10
and no Ethernet configuration. The existing NetworkManager dispatcher does not
run there. Remote guests need primary libvirt DHCP egress and the ADR-0721
source return path through the separately leased slirp NIC, independent of NIC names.

## Decision

The operator explicitly approved a Leap-only native policy amendment. Install one
KDIVE-owned Ethernet-link-type DHCPv4 policy and register it with native nanny at
startup, then explicitly enable already-present Ethernet devices through native
nanny before replay. Disable only cloud-init networking. Refuse competing foreign network
configuration; require rebuilding from the clean supported image rather than
removing it. Preserve native SSH key generation, IPv6 and non-DHCP processing.

Configure the generic updater through owned `/etc/wicked/server-local.xml` to use
the fixed private adapter `/usr/local/libexec/kdive-wicked/netconfig`: Wicked0.6.77 requires the batch command
basename `netconfig batch`. Its empty startup batch delegates successfully without
routing/replay. Delegate install/remove/batch
to `/etc/wicked/extensions/netconfig` first, preserving its arguments and DNS
processing. Parse relevant native lease/batch input as data, never shell source.
Match a validated DHCPv4 lease to its actual current interface/address. Reuse the
existing return-route helper for table/priority2291; guard removal by current
ownership. Remove only that interface's main-table DHCP default through the slirp
gateway. Preserve primary, static, connected and unrelated routes.

Use existing native-service startup integration to register policy and replay
current native lease/address state. No new daemon, timer, provider API, firewall
rule, rp_filter change, replacement network manager or Ubuntu SSH change is added.
The adapter reports/logs errors. Wicked's generic updater caller does not make
nonzero status an interface-readiness barrier; routing is not transactional.
Actual lease, route and authenticated traffic observations establish readiness.

## Consequences

The image must be rebuilt; no existing guest is silently migrated. Native initial
leases, renewal/replacement, removal/reactivation, batch and daemon restart replay,
reboot, forwarded SSH and primary egress require actual proof before completion.
Malformed/ambiguous data and foreign policy fail visibly. Tests preserve native
DNS delegation and static/unrelated routing; neither helper exit status nor a
Wicked-ready indication alone establishes the traffic contract. Partial failures
can require recovery/rebuild and retain failed evidence. Native POWER is unchanged.

## Considered & rejected

- Replace Wicked with NetworkManager — judgment: unnecessary ownership change.
- Use only up/down hooks — verified: Wicked0.6.77 `src/update.c` installs/removes
  generic lease updates through its batch action when configured; two hooks omit it.
- Treat updater failure as a readiness barrier — verified: Wicked0.6.77
  `src/ifconfig.c` accepts completed nonpositive system-update results, while
  `src/update.c` records updater failure; no such barrier is provided.
- Suppress defaults globally or select NIC names/MAC/PCI paths — judgment: discards
  primary egress or introduces topology assumptions beyond the lease-based contract.
- Inject routes through QGA after boot — judgment: no persistent native lifecycle.
