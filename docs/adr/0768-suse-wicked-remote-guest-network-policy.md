# 0768 — Leap remote guests retain native Wicked networking

## Status

Proposed

## Context

The checksum-pinned Leap15.6 image for #3082 contains Wicked0.6.77, netconfig0.85.10
and no Ethernet configuration. The existing NetworkManager dispatcher does not
run there. Remote guests need primary libvirt DHCP egress and the ADR-0721
source return path through the separately leased slirp NIC, independent of NIC names.

## Decision

The operator approved Leap-only native Ethernet DHCPv4 ownership. At existing
nanny startup enumerate actual Ethernet links with symbolic `ip -j -4 link` output.
Render one owned native hardware-class policy for each validated actual name:
`<match><class>netif-ethernet</class></match>` and DHCPv4-enabled merge. Encode only
the native policy name using pinned Wicked's `policy__` prefix, underscore doubling,
`_d` for period and `_m` for hyphen; pass ASCII alphanumerics unchanged and refuse
unsupported names before mutation. Names address discovered devices; no NIC name,
MAC or PCI topology is assumed. This is a fixed native API encoding, not a framework.

Before registration scan native policy XML in the supported nanny state directories
for selected encoded names with safe bounded reads: native loads XML names even
under different filenames. Reject duplicate/alternate-filename selected collisions. Require absent
state or identical owned origin/content with root owner0 and only validated native
UUID metadata; different or uncertain state fails visibly without alteration.
Identical persisted policy loaded at this fresh nanny startup skips registration;
no live-content export is asserted. Absent state uses installed native `busctl`
nanny `createPolicy` with the explicit XML. Any live collision fails without update.
Never use `nanny addpolicy` for replay: it transparently updates POLICY_EXISTS.
No new client library, persistence system or concurrent guest-root guarantee is added.
After create verify owned native persistence before enable, since save failure can
warn while returning success. Explicitly enable devices, then replay. Actual create/persistence/
owned replay/collision refusal and first acquisition remain required proof.

Pinned Wicked first compares encoded policy names, so generic `kdive_ethernet`
cannot apply. Its `link-type` matcher reads configured worker type, initially UNKNOWN,
rather than actual Ethernet hardware class. Native name-only diagnosis acquired no
lease; changing only its match to hardware class acquired DHCP. The native
interface-config converter is embedded in mutating addpolicy; show-policy reads
native XML unchanged and is not a read-only export of that conversion.

Startup-enumerated devices becoming ready later retain their per-device policy;
prove DEVICE_READY scheduling without startup lease waiting. New test Ethernet
fixtures receive the same owned registration before activation. This does not
promise arbitrary later production hotplug. Disable only cloud-init networking;
refuse competing configuration, preserving native SSH keys, IPv6 and netconfig.

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
- Native interface configuration through addpolicy — verified: conversion encodes
  names, but the client transparently updates existing policies; show-policy's
  native XML reader does not export that conversion. Judgment: obtaining a safe
  explicit payload through another machinery is larger than the native encoder.
- Retain generic policy — verified: native name gate rejects it; name-only native
  diagnosis remained DOWN without DHCP, so it does not satisfy first acquisition.
- Inject routes through QGA after boot — judgment: no persistent native lifecycle.
