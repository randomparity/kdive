# 0810 — Guest-egress host-loopback exposure is accepted and documented

## Status

Proposed

## Context

[ADR-0313](0313-local-libvirt-operator-gated-egress.md) lets an operator opt a local Resource into
`guest_egress`, which renders the guest's only NIC as `restrict=off`. [ADR-0804](0804-remote-authority-systems-have-only-the-forward-nic.md)
extends the same gate to remote authority Systems. With `restrict=off`, libslirp translates the
guest's connections to its gateway address (`10.0.2.2`) into connections to the host's own
`127.0.0.1`. The connection originates from the QEMU process on the host, so a network-zone
firewall never sees it, and a service bound to loopback only is reachable by an untrusted guest
kernel. Issue [#3496](https://github.com/randomparity/kdive/issues/3496) (part of #3311) asks
which of three mitigations to adopt: libvirt's passt backend, an allowlisted package proxy with
`restrict=on`, or acceptance with documentation. The documentation half is
[#3495](https://github.com/randomparity/kdive/issues/3495).

The NIC is not a libvirt `<interface>`. It is a raw `-netdev user,...,restrict=<on|off>,hostfwd=...`
on the domain's `qemu:commandline` (`src/kdive/providers/local_libvirt/lifecycle/xml.py`,
`_append_ssh_forward` and `_append_virtio_nic`). The guest return-route helpers under
`deploy/remote-libvirt-guest-helpers/` hard-code slirp's `10.0.2.0/24` lease and `10.0.2.2` gateway
([ADR-0721](0721-remote-ssh-forward-return-path-is-a-guest-source-route.md)).

### Evidence

Read from upstream source at the release tags named below, and from distribution package indexes on
2026-11-03. None of it was exercised on a running guest; the QEMU and libslirp findings are code
reads, and the table is package metadata.

**QEMU user-mode netdev has no host-loopback switch.** `NetdevUserOptions` (qapi/net.json, v10.2.2)
offers `restrict`, `ipv4`, `ipv6`, `net`, `host`, `dns`, `hostfwd`, `guestfwd` and the DHCP/TFTP/SMB
knobs, and nothing that disables the gateway-to-loopback mapping. libslirp 4.9.1 does have
`SlirpConfig.disable_host_loopback` ("Prohibit connecting to 127.0.0.1:*"), but QEMU's
`net_slirp_init` (net/slirp.c, v10.2.2 and current master) never sets it. `restrict=on` is the
only lever: it drops every guest-initiated TCP connection that is not a hostfwd or a guestfwd
target, drops all guest UDP including DNS, and drops ICMP beyond the gateway.

**libvirt's passt backend cannot close the mapping.** passt maps the guest's default-gateway
address to the host's loopback by default; `--no-map-gw` or `--map-host-loopback none` turns that
off (passt man page). libvirt 12.0.0 and current master build the passt command line from a fixed
set (`--one-off`, `--socket`, `--pid`, `--mtu`, `--interface`, `--log-file`, `--hostname`, `--fqdn`,
`--address`, `--netmask`, `--gateway`, `--tcp-ports`, `--udp-ports`) and expose neither option in
the domain XML. libvirt starts passt itself, so `qemu:commandline` cannot add one. A passt-backed
guest therefore has the same host-loopback exposure as slirp `restrict=off`.

**A guestfwd proxy path is narrow.** Under `restrict=on` slirp still honours `guestfwd` targets
(libslirp tcp_input.c: restricted connections are dropped "unless it happens to be a guestfwd").
`guestfwd=...-chardev:` binds one character device, which carries one byte stream, so concurrent
guest connections to the proxy address share it. `guestfwd=...-cmd:` starts a process per
connection, but libvirt launches QEMU with `-sandbox ...,spawn=deny` when its seccomp sandbox is
on (qemu_command.c), which is the default.

### Versions per supported host

| Host | libvirt | QEMU | passt | libslirp | libvirt passt knob for gateway mapping |
|---|---|---|---|---|---|
| Ubuntu 26.04 | 12.0.0 | 10.2.1 | git 2026-01-20 | 4.9.1 | none |
| Fedora 44 | 12.0.0 | 10.2.2 | 2026-10-02 | 4.9.1 | none |
| Rocky 10.2 / RHEL 10 | 11.10.0 | 10.1.0 (qemu-kvm) | 2025-12-10 | 4.7.0 | none |
| openSUSE Tumbleweed | 12.8.0 | 11.1.2 | 2026-10-02 | 4.9.5 | none |
| openSUSE Leap 16 (SLES 16 basis; SLES itself not checked) | 11.4.0 | 10.0.x | 2025-04-15 | 4.8.0 | none |

Every row carries libvirt at or above 9.0.0, which introduced `<backend type='passt'/>`, so passt
is available everywhere; the gap is the missing control, not the backend. Debian was not checked.
Fedora 44's values come from the installed packages; the others come from the Ubuntu, Rocky and
openSUSE package indexes. Ubuntu 24.04 already needed an AppArmor workaround for libguestfs'
passt ([ADR-0222](0222-ubuntu-build-fs-libguestfs-diagnostics.md)), a signal that confinement
policy for passt differs per distribution.

## Decision

We will accept the host-loopback exposure under `guest_egress`, document it as a residual threat
(#3495), and not change the NIC. The follow-up implementation issue
[#3497](https://github.com/randomparity/kdive/issues/3497) closes as not planned.

The exposure is a named property of `guest_egress = true`, not an unknown. An operator who enables
it accepts that an untrusted guest kernel can reach services bound to the worker host's loopback,
and must either keep such services off loopback-only trust or not enable egress on that host.

Revisit when either condition changes: QEMU exposes libslirp's `disable_host_loopback`, or libvirt
exposes passt's `--no-map-gw` (or `--map-host-loopback`). Either makes a one-flag mitigation that
keeps the current NIC shape.

## Consequences

- No code, schema, migration or tool change. `restrict=on` stays the default; the default path is
  unaffected.
- The `guest_egress` threat statement in ADR-0313 stays as written; #3495 adds the loopback
  specifics to it and to the runbooks.
- The host-side controls available today are operator-owned and untested by this record. The
  worker slot accounts own the QEMU processes, so a firewall rule scoped to those uids and to new
  loopback connections is a candidate recipe. It would need a live proof on a lab host before any
  runbook recommends it, and it does not cover a deployment whose QEMU shares a uid with kdive's
  own services.
- ADR-0721's dispatcher and the three return-route helpers are untouched.

## Considered & rejected

- **libvirt passt backend.** Rejected: it does not mitigate. passt's default gateway mapping is the
  same exposure, and libvirt 12.0.0 and master offer no way to turn it off. Adopting it would also
  replace the raw hostfwd with `<portForward>`, move the guest onto host-derived addressing so the
  ADR-0721 dispatcher and the `10.0.2.2` helpers stop matching, and add per-distribution
  AppArmor/SELinux policy for passt. If a libvirt release exposes `--no-map-gw`, the mitigation
  becomes worth that cost and the decision is revisited.
- **Spawning passt from kdive.** Rejected: it gives the control but moves passt's lifecycle,
  confinement and cleanup onto kdive, which the provider has no host-process channel for
  (rejected on the same ground for the host relay in ADR-0721).
- **Allowlisted package proxy with `restrict=on` and `guestfwd`.** Rejected for now. It does close
  the loopback path, and it makes egress stricter than today's open NAT. But the chardev form
  serializes guest connections, the per-connection `cmd:` form needs the seccomp spawn denial and
  the QEMU confinement relaxed, and the guest then needs proxy configuration baked into every
  image. The operator must also run and keep an allowlist current for each distribution's mirrors.
  It turns `guest_egress` from a boolean into a proxy-only mode, which is the general egress policy
  model change excluded from #3496.
- **Patch libslirp or QEMU.** Out of scope: the switch exists in libslirp; exposing it is an
  upstream QEMU change kdive can only ask for.
