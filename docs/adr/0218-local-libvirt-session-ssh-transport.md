# ADR 0218 — Local-libvirt drgn-live SSH transport over a loopback-forwarded guest SSH port

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Issue:** [#697](https://github.com/randomparity/kdive/issues/697) (M2.8 Epic B, blocks B3 #677)
- **Refines:** [ADR-0039](0039-ssh-transport-live-introspection.md) (the SSH Connect transport +
  secret-by-reference credential contract), [ADR-0209](0209-capability-aware-mcp-admission.md)
  (the fail-fast that gates a `drgn-live` request until the descriptor advertises it),
  [ADR-0210](0210-local-libvirt-live-debug-introspection.md) §1 (the local production
  transport-resolution decision; this ADR is its concrete realization for the SSH/`drgn-live`
  half that B1 left out).
- **Builds on:** [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the loopback-before-IO
  bounded connect contract `_open_ssh` already enforces), [ADR-0052](0052-bootable-rootfs-image-builder.md)
  (the in-guest sshd + managed authorized key the rootfs build already installs),
  [ADR-0085](0085-drgn-live-transport-generalization.md) (the `drgn-live` agent token whose
  local realization is SSH).
- **Spec:** [`docs/specs/2026-06-23-local-libvirt-session-ssh-transport.md`](../specs/2026-06-23-local-libvirt-session-ssh-transport.md).

## Context

B1 (#675, ADR-0210 §1) wired local-libvirt's **gdbstub** transport: the provisioner bind-probes a
free loopback port, records `-gdb tcp:127.0.0.1:<port>` into the domain XML, and
`_real_resolve_endpoint` reads it back. It shipped gdbstub **only** —
`supported_debug_transports = frozenset({"gdbstub"})` — so a `drgn-live` request fail-fasts with
`capability_unsupported` (ADR-0209) before reaching `_real_resolve_ssh_endpoint`, which is a stub
raising a `CONFIGURATION_ERROR` that points at this issue.

The local `drgn-live` transport realizes over **drgn-over-SSH into an in-guest sshd** (ADR-0039),
not over the qemu-guest-agent the remote provider uses. Three pieces are needed and two of the
three already exist on `main`:

1. **In-guest sshd + authorized key — already exists.** `rootfs_build.py` installs
   `openssh-server`, runs `systemctl enable sshd.service`, and `--ssh-inject`s the kdive-managed
   public key (ADR-0052). It is enabled at build time; nothing in the boot path needs to change
   for sshd to start under direct-kernel boot (sshd is a normal `multi-user.target` unit).
2. **Credential plumbing — already exists.** `debug.start_session` already resolves the profile's
   `ssh_credential_ref` through the bound secret backend **before** `open_transport`, registering
   the value into the redaction registry (ADR-0039 §2, `sessions_lifecycle._resolve_credential`),
   gated on `ProfilePolicy.drgn_live_requires_credential` (local returns `True`). The connect-plane
   `_open_ssh` orchestration (loopback-before-IO, SSH reachability probe, `ssh://` handle) is real
   and unit-tested.
3. **The forwarded SSH port — does not exist on `main`.** The local domain has **no network
   interface** at all (direct-kernel whole-disk-ext4, no NIC), so there is no path to the guest's
   port 22. Nothing renders a host→guest SSH forward, and `_real_resolve_ssh_endpoint` has no port
   to read back. This is the gap this ADR closes.

## Decision

Render a loopback-only QEMU user-mode SSH port-forward into the local domain XML, record the
forwarded host port the same way B1 records the gdbstub port, resolve it back in
`_real_resolve_ssh_endpoint`, and advertise `drgn-live` in the capability descriptor. The
mechanism mirrors B1's gdbstub-port path end-to-end so there is one shape, not two.

### 1. Provision renders a loopback SSH forward and a guest NIC

A `drgn-live`-capable System is one whose profile sets `ssh_credential_ref` (the only signal that
the operator opted into live SSH introspection; a profile without it never realizes drgn-live —
ADR-0039 §2 / the `LibvirtProfile.ssh_credential_ref` contract). When (and only when)
`ssh_credential_ref` is set, `render_domain_xml` renders, via the existing `<qemu:commandline>`
passthrough (the same `QEMU_NS` / `register_qemu_namespace()` helpers B1 and remote use):

```xml
<qemu:commandline>
  <qemu:arg value="-netdev"/>
  <qemu:arg value="user,id=kdivessh,hostfwd=tcp:127.0.0.1:<port>-:22"/>
  <qemu:arg value="-device"/>
  <qemu:arg value="virtio-net-pci,netdev=kdivessh"/>
</qemu:commandline>
```

`-netdev user` is QEMU's built-in unprivileged SLIRP user-mode network (no host bridge, no root,
no extra daemon); `hostfwd=tcp:127.0.0.1:<port>-:22` forwards a **loopback-only** host port to the
guest's sshd. The `127.0.0.1` literal is hard-coded: local-libvirt is single-host and the loopback
bind is the security boundary, identical to the gdbstub `-gdb tcp:127.0.0.1:<port>` rule and
mirrored by `_is_loopback_literal` enforcement at connect time. The guest brings the NIC up by
DHCP (SLIRP's built-in DHCP server hands the guest `10.0.2.15`); the kdive-ready rootfs already
enables `systemd-networkd`/`NetworkManager` defaults sufficient for a single DHCP NIC, confirmed
on the live drive (B6).

`render_domain_xml` gains a keyword-only `ssh_port: int | None = None`. Passing `ssh_port=None`
with `ssh_credential_ref` set is a programming error (the provisioner always allocates when the
flag is set) and raises `CONFIGURATION_ERROR`; `ssh_port` is ignored when no credential ref is
set. A System may carry **both** the gdbstub `-gdb` arg and the SSH `-netdev`/`-device` args in one
`<qemu:commandline>` element (gdbstub and drgn-live are different transports for different ops,
ADR-0039 §4); the renderer appends both sets of args to a single commandline element.

### 2. Port allocation reuses B1's idempotent bind-probe

`provision()` is idempotent (a retry redefines the domain). SSH-port allocation reuses B1's exact
mechanism so a retry records the *same* port the already-running QEMU forwards:

1. If the System's domain is already defined **and** records a forwarded SSH port, **reuse** it
   (read it back from the existing domain `XMLDesc()`).
2. Otherwise bind-probe a fresh loopback port (`bind(("127.0.0.1", 0))`, read `getsockname()`,
   close, render). The brief release-then-QEMU-bind TOCTOU window is accepted exactly as for
   gdbstub: loopback, single-host, single-attach, and a collision surfaces as a clean
   `PROVISIONING_FAILURE` on domain start that the transactional `_define_and_start` already
   converts to a failure + undefine.

The existing `_bind_probe_free_port` seam (the injected `free_port` callable) is reused for **both**
ports; a gdbstub-and-ssh System bind-probes twice (two independent free ports). The recorded-port
read is a new shared `recorded_ssh_port` reader (decision 5) mirroring `recorded_gdb_port`.

### 3. Install preserves the SSH forward across the direct-kernel re-define

`install._render_os_section` already calls `register_qemu_namespace()` before `ET.tostring` (the
B1 fix for the `<ns0:commandline>` prefix-strip hazard, ADR-0210 §2a). The SSH args live in the
**same** `<qemu:commandline>` element as the gdbstub arg, so that existing fix already preserves
them — no new install edit. A unit test runs a provision XML carrying the SSH forward through the
install os-edit and asserts the `qemu:`-prefixed `-netdev ... hostfwd ...` arg survives, pinning
the regression guard for the element this ADR introduces.

### 4. Resolve reads the recorded forwarded port back

`_real_resolve_ssh_endpoint(system)` (today a `CONFIGURATION_ERROR` stub) becomes a real resolver
that mirrors `_resolve_endpoint_via` exactly: connect to libvirt, `lookupByName(str(system))`,
read `XMLDesc()`, parse the forwarded SSH port with the shared `recorded_ssh_port` reader, and
return `("127.0.0.1", port)`. It feeds the existing loopback-enforcing `_open_ssh` probe unchanged.

Error contract (identical shape to the gdbstub resolver):
- Domain not found / not running → `CONFIGURATION_ERROR` ("System has no running libvirt domain").
- Domain exists but records **no** forwarded SSH port (System provisioned without a credential
  ref) → `CONFIGURATION_ERROR` ("System was not provisioned for drgn-live; reprovision with
  `ssh_credential_ref` set"). **Not** `MISSING_DEPENDENCY`.
- libvirt connect / XML-read fault → `INFRASTRUCTURE_FAILURE`.
- Malformed XML → `INFRASTRUCTURE_FAILURE`.

The libvirt connect + `XMLDesc` read is the only `# pragma: no cover - live_vm` seam; the
host:port composition and every error branch are unit-tested with a fake connection. The real
SSH connect (`_real_ssh_connect`) stays `live_vm`-gated — it needs a booted guest and the resolved
credential (already registered by the caller).

### 5. Shared `recorded_ssh_port` reader

`providers/shared/libvirt_xml.py` already hosts `recorded_gdb_port`/`recorded_gdb_port_from_root`
(promoted there in B1). Add a sibling `recorded_ssh_port` / `recorded_ssh_port_from_root` that
walks the same `<qemu:arg>` list for a `hostfwd=tcp:127.0.0.1:<port>-:22` value and returns the
forwarded host port. It is the bare `(xml) -> int | None` parser; the resolver wraps it with
operation/domain detail, mirroring the gdbstub reader. Both providers share `QEMU_NS`; only local
forwards SSH today (remote reaches the guest over the guest agent), so the reader lives in shared
for symmetry with its gdbstub sibling, not because remote calls it yet.

### 6. Descriptor flip is live-gated; tool maturity stays `partial`

`composition.py` adds `"drgn-live"` to `supported_debug_transports` so capability-aware admission
(ADR-0209) admits a `drgn-live` `debug.start_session` on local and the credential/transport path
runs end-to-end. **`debug.*` tool maturity stays `partial`** (per ADR-0208 invariant 5: maturity
asserts the plane works *on hardware*; CI proves only the fake-seam contract — no KVM, no booted
guest, no real sshd). The descriptor advertises the *wired* capability; the milestone live-verifier
(B6 #680) promotes maturity only after a live `debug.start_session(run, "drgn-live")` → drgn attach
→ helper round-trip on the KVM host. This is the same descriptor-vs-maturity split B2 (#676) and B4
(#678) used: flip the descriptor in-code when the seam is wired, hold the maturity promotion for
the live proof.

This deviates from ADR-0210 §1's literal "flip the descriptor **and** promote maturity in the same
PR" wording in the conservative direction only: ADR-0210 assumed the wiring PR would itself carry
the live drive, but this milestone separated the live proof into B6, so the wiring PR flips the
descriptor (the capability is genuinely wired) while B6 owns the maturity promotion. The honesty
invariant ADR-0208 protects — never advertise a stubbed plane as working — holds: a `partial`
maturity is the honest signal that the live proof is outstanding.

## Consequences

- `debug.start_session(run, "drgn-live")` on a `ssh_credential_ref`-provisioned local System opens
  a real loopback SSH transport in production: the credential resolves and registers, the resolver
  reads the forwarded port, `_open_ssh` enforces loopback and probes reachability. This unblocks
  B3 (#677), live `introspect.run`, which attaches drgn over this transport.
- A System provisioned **without** `ssh_credential_ref` gets an actionable `CONFIGURATION_ERROR`
  on a `drgn-live` attach (no forwarded port), not an opaque `MISSING_DEPENDENCY`.
- The local domain gains a single loopback-only user-mode NIC **only** for SSH-capable Systems; a
  gdbstub-only or plain System renders no NIC (unchanged boot, no new attack surface).
- No port, schema, or migration change — the seams satisfy the existing `Connector` port; the
  change is XML rendering + port allocation + the resolver + the descriptor flip.
- Credential material never touches the domain XML, a state row, or a response: the `hostfwd` rule
  carries only a port, and the credential resolves at the worker boundary through the redaction
  registry (the contract ADR-0039 §2 already enforces and this ADR reuses unchanged).

## Considered & rejected

- **`passt` instead of QEMU `-netdev user` (SLIRP).** Rejected: `passt` is a separate userspace
  daemon that must be installed, launched, and (on Ubuntu) reconciled with an apparmor profile
  (the #694 build-fs friction). `-netdev user` is built into QEMU, unprivileged, needs no daemon,
  and the loopback `hostfwd` is exactly the forward we want. passt's performance edge is
  irrelevant for a single low-traffic SSH control channel. The issue lists both and lets the ADR
  pick; hostfwd is simpler with no operator-visible prerequisite.
- **A second `<interface>`/libvirt-native network instead of the qemu commandline passthrough.**
  Rejected: a libvirt `<interface type='user'>` does not expose `hostfwd` in the domain schema
  (port-forward config is a QEMU user-net feature libvirt does not model), so the loopback forward
  has to go through the `<qemu:commandline>` passthrough regardless — and that passthrough is the
  exact channel B1 already uses for `-gdb`, so the recording/round-trip machinery is shared.
- **A stored System config field for the SSH port instead of the live domain XML.** Rejected for
  the same reason ADR-0210 rejected it for gdbstub: the live domain XML the provider defined is the
  authoritative running-state source and already encodes the forwarded port; a second source could
  drift from the port QEMU actually forwards. Read the domain.
- **Promote `debug.*` maturity to `implemented` on merge.** Rejected per ADR-0208 invariant 5: a
  fakes-only CI run (no KVM, no booted sshd) never proves the plane works; the live drive (B6 #680)
  owns the promotion. Advertising `drgn-live` in the descriptor while holding maturity at `partial`
  is the honest in-between: wired, not yet hardware-proven.
- **Wire B3 (`introspect.run` live drgn) in this PR.** Rejected: #677 is a separate issue that
  consumes this transport. This PR ships the transport and leaves `supported_introspection`
  untouched (`introspect.run` `live` mode stays unadvertised until B3 wires the live drgn seam).
- **A configured SSH port range (remote's `gdbstub_range` shape).** Rejected as over-built for
  single-host loopback, exactly as B1 rejected it for the gdbstub port: a bind-probe needs no
  operator config or reservation policy; the port is loopback-private.
