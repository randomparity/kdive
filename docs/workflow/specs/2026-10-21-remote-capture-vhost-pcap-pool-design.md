# Remote traffic capture: vhost-off data NIC and a provisioned pcap pool (#3316)

Decision record: [ADR-0432](../../adr/0432-remote-libvirt-traffic-capturer.md), amended by this
change. Local equivalent: [ADR-0385](../../adr/0385-host-side-traffic-capture.md) and the
[ADR-0640](../../adr/0640-static-svirt-image-label-for-session-mode-domains.md) label. Builds on #3294, which
makes QEMU's refusal reach the capturer as a parsed QMP reply.

## Problem

Remote `control.capture_traffic` cannot succeed on a standard remote provider host. Two separate
refusals block it, both measured on a Rocky Linux 10 provider with SELinux enforcing:

1. **vhost-net.** libvirt enables vhost-net for the virtio `type='network'` data NIC. QEMU's
   `netfilter_complete()` (`net/filter.c`) refuses every netfilter on a vhost-backed netdev with
   `Vhost is not supported`, so `filter-dump`, `filter-mirror` and the rest all fail. libvirt cannot
   change the NIC driver on a running domain (`qemuDomainChangeNet`: "cannot modify virtio network
   device driver attributes").
2. **pcap destination.** The capturer writes into the operator `storage_pool` directory. That
   directory is `root:root 0711` and `virt_image_t:s0`; the confined QEMU runs as the QEMU runtime
   user in `svirt_t` with per-domain MCS categories. Both the DAC and the SELinux check deny the
   create (`net dump: can't open …: Permission denied`). `REMOTE_PCAP_WRITE_REMEDIATION` says the
   pool is already QEMU-writable, which is false. On an AppArmor host the per-domain profile that
   `virt-aa-helper` generates does not list the pcap path either.

## Decision (operator, 2026-10-21)

### Data NIC: vhost off, always

The ordinary remote renderer adds `<driver name='qemu'/>` to the libvirt `<interface>`, so the data
NIC uses QEMU's userspace virtio-net backend and accepts the netfilter. It applies to every ordinary
remote System, not only to Systems that will capture: capture is a runtime tool, vhost is fixed at
define time, and a per-System opt-in would add a provision-time field. The authority path renders no
`<interface>` (ADR-0804) and is unchanged. The SSH forward and the gdbstub do not use this NIC.

A System defined before this change keeps vhost. Its capture now fails with a non-retryable
`CONFIGURATION_ERROR` `reason=capture_netdev_vhost` whose remediation is to reprovision the System,
instead of the retryable `CONTROL_FAILURE` `capture_filter_rejected`. The match is on QEMU's reply
description; only the reason token reaches the error details.

### pcap destination: a dedicated, provisioned pool

A new setting `KDIVE_REMOTE_LIBVIRT_PCAP_POOL` (default `kdive-pcap`) names a libvirt `dir` pool that
holds remote pcaps. The gated capture child receives it in the staged `RemoteCaptureConfiguration`
(defaulted, so configuration staged before the field still parses). The capturer's `prepare`, `captured_size`, `fetch` and `reclaim`, the ADR-0556
capture reaper and the live-stack lane spool use it in place of `storage_pool`. A missing pool is a
`CONFIGURATION_ERROR` `reason=pcap_pool_missing` naming the setup step, not an
`INFRASTRUCTURE_FAILURE`.

`libvirt_pool_net` provisions the pool on every host it already prepares:

- directory `/var/lib/libvirt/kdive-pcap`, owned by the QEMU runtime user of the host's family
  (`qemu` on RedHat and SUSE, `libvirt-qemu` on Debian) with group `root`, mode `0770`. The group
  is not the QEMU user's primary group: on Debian that is `kvm`, whose members include the provider
  authority account, which must not read other Systems' captures;
- the pool XML carries the same owner, group and mode in `<permissions>`, so a pool build does not
  reset the directory to libvirt's `0711` default;
- the directory sits outside `/var/lib/libvirt/kdive`, whose boot-artifact pool leaves it
  `root:libvirt 0770`, and the role checks with `runuser` that the QEMU runtime user can write it;
- on a host whose `getenforce` reports `Enforcing` (`libvirt_stack` installs `sefcontext`'s bindings
  on the RedHat and SUSE families): an `svirt_image_t` fcontext rule for the directory
  and a plain `restorecon -R` (not `-F`), as ADR-0640 does locally. Every domain reaches `s0` by MCS
  dominance; the file QEMU creates inherits `svirt_image_t` at that domain's own categories, so
  another domain cannot read or unlink it. The root storage daemon already reads category-labelled
  files in pools (running disk images), which is how the fetch-back and reclaim reach the pcap;
- on a host with libvirt's AppArmor abstraction: a drop-in
  `/etc/apparmor.d/abstractions/libvirt-qemu.d/kdive-pcap` allowing `rw` on
  `<dir>/kdive-pcap-*.pcap`. Upstream libvirt's abstraction ends with
  `include if exists <abstractions/libvirt-qemu.d>`; the role fails with a remediation when the
  installed abstraction lacks that include, rather than writing a rule nothing loads. The rule takes
  effect for domains started after it is written. AppArmor has no MCS, so on such a host any
  domain's QEMU may write any matching pcap in the directory. That residual is recorded in the ADR.

`REMOTE_PCAP_WRITE_REMEDIATION` names the pool, its owner and label requirements, and the
`site.yml` step that provisions it.

### Rejected

- **vhost off only for capture-enabled Systems:** needs a provision-time request field, and the agent
  would have to predict a capture.
- **Capturing outside QEMU** (`tcpdump` or `tc mirred` on the host tap): needs a host shell transport,
  which ADR-0432 rejected; the libvirt bridge would also carry other tenants' traffic.
- **Capturing the `kdivessh` netdev:** it carries only the restricted SSH forward.
- **Relabelling the operator `storage_pool` `svirt_image_t`:** every resting disk image would become
  writable by every confined domain.
- **libvirt's per-domain private directory:** already labelled for the domain, but reachable for
  fetch-back only through a pool over a libvirt-internal directory that also holds the domain's
  master key.

## Interfaces

- New setting `KDIVE_REMOTE_LIBVIRT_PCAP_POOL` (`remote-libvirt` group, worker and reconciler),
  `RemoteLibvirtConfig.pcap_pool`; generated config reference regenerated.
- New error reasons `capture_netdev_vhost` and `pcap_pool_missing`, both `CONFIGURATION_ERROR`.
- Rendered remote domain XML gains the interface driver element.
- `libvirt_pool_net` gains a pool, and its variables `libvirt_pool_net_pcap_pool_*`.
- No migration, no agent-facing tool change.

## Upgrade

Rerun `site.yml` on each remote provider host to provision the pool, and reprovision existing remote
Systems to drop vhost. A capture on an unprovisioned host or an old System fails with the matching
configuration error and remediation.

## Failure model

- Pool absent or not a `dir`/`fs`/`netfs` pool: `CONFIGURATION_ERROR` before any QMP command.
- Old System with vhost: `CONFIGURATION_ERROR` `capture_netdev_vhost` at `object-add`; nothing
  written, nothing to reclaim.
- Pool provisioned but the label or the AppArmor rule missing: `CONFIGURATION_ERROR`
  `pcap_open_denied` (#3294) carrying the rewritten remediation.
- Reaper: an orphaned capture's volume is deleted from the pcap pool; a missing pool defers the row
  as the existing infrastructure path does.

## Tests

- Renderer: the ordinary path emits `<driver name='qemu'/>` inside the interface.
- Capturer: every file primitive resolves the pcap pool; a missing pool raises
  `pcap_pool_missing` with remediation; a vhost reply raises `capture_netdev_vhost` and leaks no
  description text.
- Reaper: deletes from the pcap pool.
- Gated child: the configured pool travels in the staged capture configuration, and configuration
  staged without it parses to the default.
- Settings and config: field defaults and parsing.
- Role: the structural checks in `deploy/ansible/tests/` (pool activated and autostarted by separate
  tasks, permission templates in the pool XML, fcontext and relabel gated on `Enforcing`,
  the QEMU-user access check, the target outside the boot-artifact parent, AppArmor tasks gated on the
  abstraction), and the regenerated runner task fixture.
- Live: the 4 remote `control.capture_traffic` functional cells on an SELinux-enforcing provider, one
  capture on an Ubuntu AppArmor provider, and an iperf3 vhost on/off throughput measurement.
