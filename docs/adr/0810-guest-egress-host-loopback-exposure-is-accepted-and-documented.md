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
kernel. That includes kdive's own per-System loopback listeners: every System's SSH `hostfwd`
(`127.0.0.1:<port>`) and, when enabled, its gdbstub (`-gdb tcp:127.0.0.1:<port>`, no authentication).
An egress-enabled guest can therefore probe other guests' SSH and gdbstub ports on the same host. Issue [#3496](https://github.com/randomparity/kdive/issues/3496) (part of #3311) asks
which of three mitigations to adopt: libvirt's passt backend, an allowlisted package proxy with
`restrict=on`, or acceptance with documentation. The documentation half is
[#3495](https://github.com/randomparity/kdive/issues/3495).

The customization-boot egress NIC (`user,id=kdivebuild,restrict=off`, `_append_egress_nic`) is not
gated by `guest_egress` and has the same mapping; it runs build-time code and is covered by this
decision as well.

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
off (passt man page); it is also off when the host has no default route. libvirt 12.0.0 and current
master (qemu_passt.c) build the passt command line from a fixed set of options and expose neither
`--no-map-gw` nor `--map-host-loopback` in the domain XML. libvirt starts passt itself, so
`qemu:commandline` cannot add one. A libvirt passt-backed guest therefore has the same
host-loopback exposure as slirp `restrict=off`.

**QEMU's own passt netdev has the control, and the libvirt sandbox blocks it.** QEMU 10.1 added
`-netdev passt` with `map-host-loopback` and `tcp-ports` options (qapi/net.json `NetDevPasstOptions`,
"Since: 10.1"); it is built into the Fedora 44 QEMU. Because it is a netdev, it could ride the same
`qemu:commandline` route as today's NIC. QEMU starts passt itself (net/passt.c,
`g_subprocess_launcher_spawnv`). Checked on the Fedora 44 host with a bare `-machine none` QEMU:
`-netdev passt,id=n0,map-host-loopback=none,tcp-ports=127.0.0.1/22222:22` starts with the sandbox
off, and fails with `Error creating daemon: Failed to fork (Operation not permitted)` under
`-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny`, the argument
libvirt adds by default. That is a start-up check only: no guest booted, and the effect of
`map-host-loopback=none` on guest traffic was not exercised.

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
| Fedora 44 (installed, verified) | 12.0.0 | 10.2.2 | 2026-10-02 | 4.9.1 | none |
| Rocky 10.2 / RHEL 10 | 11.10.0 | 10.1.0 (qemu-kvm) | 2025-12-10 | 4.7.0 | none |
| openSUSE Tumbleweed | 12.8.0 | 11.1.2 | 2026-10-02 | 4.9.5 | none |
| openSUSE Leap 16 (SLES 16 basis; SLES itself not checked) | 11.4.0 | 10.0.x | 2025-04-15 | 4.8.0 | none |

QEMU's own passt netdev needs QEMU 10.1 or later, which leaves out Leap 16; whether the Ubuntu,
Rocky and openSUSE QEMU builds compile it in was not checked.

Every row carries libvirt at or above 9.0.0, which introduced `<backend type='passt'/>` (libvirt
formatdomain, "Since 9.0.0"), so passt is available everywhere; the gap is the missing control, not the backend. Debian was not checked.
The non-Fedora rows are package-index metadata, not installed and run. Ubuntu 24.04 already needed an AppArmor workaround for libguestfs'
passt ([ADR-0222](0222-ubuntu-build-fs-libguestfs-diagnostics.md)), a signal that confinement
policy for passt differs per distribution.

## Decision

We will accept the host-loopback exposure under `guest_egress`, document it as a residual threat
(#3495), and not change the NIC. The follow-up implementation issue
[#3497](https://github.com/randomparity/kdive/issues/3497) closes as not planned.

The exposure is a named property of `guest_egress = true`, not an unknown. An operator who enables
it accepts that an untrusted guest kernel can reach services bound to the worker host's loopback,
and must either keep such services off loopback-only trust or not enable egress on that host.

Revisit when any of these changes: QEMU exposes libslirp's `disable_host_loopback`; libvirt exposes
passt's `--no-map-gw` or `--map-host-loopback`; or QEMU's own passt netdev becomes usable under
libvirt's sandbox. Each gives a mitigation that keeps the current `qemu:commandline` NIC shape.

## Consequences

- No code, schema, migration or tool change. `restrict=on` stays the default; the default path is
  unaffected.
- The `guest_egress` threat statement in ADR-0313 stays as written; #3495 adds the loopback
  specifics to it and to the runbooks.
- Host-side controls are operator-owned and untested by this record. The fixed worker slot accounts
  run the session libvirt daemon, so QEMU runs under them; a firewall rule scoped to those uids and
  to new loopback connections is a candidate recipe. It needs a live proof on a lab host before any
  runbook recommends it, and it does not apply where QEMU shares a uid with kdive's own services.
  Running with no default route on the host also disables passt's mapping, but not slirp's.
- ADR-0721's dispatcher and the three return-route helpers are untouched.

## Considered & rejected

- **libvirt passt backend.** Rejected: it does not mitigate. passt's default gateway mapping is the
  same exposure, and libvirt 12.0.0 and master offer no way to turn it off. Adopting it would also
  replace the raw hostfwd with `<portForward>`, bring the addressing change below, and add
  per-distribution AppArmor/SELinux policy for passt. If a libvirt release exposes `--no-map-gw`, the mitigation
  becomes worth that cost and the decision is revisited.
- **QEMU's `-netdev passt,map-host-loopback=none`.** Rejected: it keeps the NIC shape and closes the
  mapping, but QEMU forks passt, so it works only with libvirt's seccomp sandbox (`spawn=deny`)
  disabled. `seccomp_sandbox` is a daemon-wide libvirt setting, so the change would weaken QEMU's
  containment for every guest running an untrusted kernel, trading a smaller boundary for a larger
  one. It also needs QEMU 10.1 or later (not Leap 16), a MAC-policy answer for QEMU executing passt
  on each distribution, and a move to host-derived guest addressing (below).
- **A passt started outside QEMU** (a per-System service, joined by `-netdev stream`). This gives
  the control without the sandbox change, and it is the strongest alternative to accepting the
  exposure. Rejected for now: kdive would own passt's lifecycle, cleanup and confinement per
  System, which the provider has no channel for (the same ground on which ADR-0721 rejected a host
  relay), and it carries the addressing change below. It is the option to cost if an operator
  requires the mitigation.
- **Addressing change common to every passt route.** The guest receives host-derived addresses
  instead of slirp's `10.0.2.15` lease and `10.0.2.2` gateway. The ADR-0721 dispatcher and the
  three return-route helpers key on that subnet and would stop matching, and the SSH `hostfwd`
  becomes a passt port-forward whose source address as seen by the guest was not examined.
- **Allowlisted package proxy with `restrict=on` and `guestfwd`.** Rejected for now. It does close
  the loopback path, and it makes egress stricter than today's open NAT. But the chardev form
  serializes guest connections, the per-connection `cmd:` form needs the seccomp spawn denial and
  the QEMU confinement relaxed, and the guest then needs proxy configuration baked into every
  image. The operator must also run and keep an allowlist current for each distribution's mirrors.
  It turns `guest_egress` from a boolean into a proxy-only mode, which is the general egress policy
  model change excluded from #3496.
- **Patch libslirp or QEMU.** Out of scope: the switch exists in libslirp; exposing it is an
  upstream QEMU change kdive can only ask for.
