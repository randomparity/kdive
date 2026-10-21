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

- directory `/var/lib/libvirt/kdive-pcap`, `root:root 0711`, with the same permissions in the pool
  XML `<permissions>`;
- the directory sits outside `/var/lib/libvirt/kdive`, whose boot-artifact pool leaves it
  `root:libvirt 0770`, and the role checks with `runuser` that the family's QEMU runtime user
  (`qemu` on RedHat and SUSE, `libvirt-qemu` on Debian) can search it and cannot write it;
- on a host whose `getenforce` reports `Enforcing` (`libvirt_stack` installs `sefcontext`'s bindings
  on the RedHat and SUSE families): an `svirt_image_t` fcontext rule for the directory
  and a plain `restorecon -R` (not `-F`), as ADR-0640 does locally, so the confined QEMU may write
  the pre-created `svirt_image_t:s0` file;
- on a host with libvirt's AppArmor abstraction: a drop-in
  `/etc/apparmor.d/abstractions/libvirt-qemu.d/kdive-pcap` allowing only `w` on
  `<dir>/kdive-pcap-*.pcap`. Upstream libvirt's abstraction ends with
  `include if exists <abstractions/libvirt-qemu.d>`; the role fails with a remediation when the
  installed abstraction lacks that include, rather than writing a rule nothing loads. The rule takes
  effect for domains started after it is written.

QEMU does not create the capture file. Just before `object-add`, `attach` creates the job's volume
with `pool.createXML`, owned by the uid and gid of the running domain's live DAC seclabel
(`<seclabel model='dac'><label>+uid:+gid</label>`) with mode `0600`; the `filter-dump` opens that
existing file. With no write on the directory, QEMU cannot unlink the file or put a symlink in its
place, so the root storage daemon, which resolves volume paths and follows non-dangling links, never
reads a host file of QEMU's choosing on fetch-back. A missing, malformed or root (`+0`) seclabel
fails `CONFIGURATION_ERROR` `reason=dac_seclabel_unusable`; the uid is never guessed. A failed
`object-add` deletes the pre-created volume. The cost: the file is `svirt_image_t:s0`, not the
domain's MCS categories, so under SELinux (and under AppArmor, which has no categories) another
compromised QEMU could open an in-flight capture whose name it guesses; the `0711` directory denies
listing. The ADR records the refuted QEMU-owned-directory shape and this residual.

`REMOTE_PCAP_WRITE_REMEDIATION` names the pool, its ownership and label requirements, and the
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

- Pool absent, stopped, or not a `dir`/`fs`/`netfs` pool: `CONFIGURATION_ERROR` before any QMP
  command.
- No usable DAC seclabel on the running domain: `CONFIGURATION_ERROR` `dac_seclabel_unusable`
  before any volume is created or QMP command sent.
- Volume creation refused by the storage daemon: `INFRASTRUCTURE_FAILURE`; no filter is added.
- `object-add` refused or failing after the volume exists: the volume is deleted, then the error
  propagates.
- Old System with vhost: `CONFIGURATION_ERROR` `capture_netdev_vhost` at `object-add`; nothing
  written, nothing to reclaim.
- Pool provisioned but the label or the AppArmor rule missing: `CONFIGURATION_ERROR`
  `pcap_open_denied` (#3294) carrying the rewritten remediation.
- Reaper: an orphaned capture's volume is deleted from the pcap pool; a missing pool defers the row
  as the existing infrastructure path does.

## Tests

- Renderer: the ordinary path emits `<driver name='qemu'/>` inside the interface.
- Capturer: every file primitive resolves the pcap pool; a missing or stopped pool raises
  `pcap_pool_missing` or `pcap_pool_inactive` with remediation; a vhost reply raises
  `capture_netdev_vhost` and leaks no description text; `attach` pre-creates the volume owned by the
  domain's DAC uid:gid with mode 0600, refuses a missing, malformed or root seclabel without creating
  anything, and deletes the volume when `object-add` fails.
- Reaper: deletes from the pcap pool.
- Gated child: the configured pool travels in the staged capture configuration, and configuration
  staged without it parses to the default.
- Settings and config: field defaults and parsing.
- Role: the structural checks in `deploy/ansible/tests/` (pool activated and autostarted by separate
  tasks, the directory and pool XML root:root 0711, the QEMU user able to search but not write it,
  a write-only AppArmor rule, fcontext and relabel gated on `Enforcing`,
  the QEMU-user access check, the target outside the boot-artifact parent, AppArmor tasks gated on the
  abstraction), and the regenerated runner task fixture.
- Live: the 4 remote `control.capture_traffic` functional cells on an SELinux-enforcing provider, one
  capture on an Ubuntu AppArmor provider, and an iperf3 vhost on/off throughput measurement. On both
  MAC systems: the pre-created file's label and owner, QEMU's write into it, and a refused symlink
  swap in the directory as the QEMU user.
