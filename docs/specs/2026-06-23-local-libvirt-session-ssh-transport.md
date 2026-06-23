# Spec — Local-libvirt drgn-live SSH transport (M2.8, #697)

- **Status:** Draft
- **Date:** 2026-06-23
- **Issue:** [#697](https://github.com/randomparity/kdive/issues/697) (M2.8 Epic B; blocks B3 #677)
- **ADR:** [ADR-0218](../adr/0218-local-libvirt-session-ssh-transport.md) (this spec is its
  concrete realization). Refines [ADR-0039](../adr/0039-ssh-transport-live-introspection.md) (the
  SSH transport + secret-by-reference credential contract),
  [ADR-0209](../adr/0209-capability-aware-mcp-admission.md) (capability-aware admission),
  [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) §1 (local production
  transport resolution). Mirrors the B1 gdbstub spec
  [`2026-06-22-local-libvirt-gdbstub-transport-resolution.md`](2026-06-22-local-libvirt-gdbstub-transport-resolution.md).
- **Design:** [M2.8 local-libvirt service parity](../design/m2.8-local-libvirt-service-parity.md).

## Context

B1 (#675) wired local-libvirt's gdbstub transport: provision bind-probes a free loopback port,
records `-gdb tcp:127.0.0.1:<port>` into the domain XML through the `<qemu:commandline>`
passthrough, and `_real_resolve_endpoint` reads it back, feeding the loopback-enforcing
`_open_gdbstub` probe. It shipped gdbstub **only**:
`supported_debug_transports = frozenset({"gdbstub"})`, so a `drgn-live` request fail-fasts with
`capability_unsupported` (ADR-0209) before reaching `_real_resolve_ssh_endpoint` — a stub raising
a `CONFIGURATION_ERROR` that points at this issue.

The local `drgn-live` transport realizes over **drgn-over-SSH into an in-guest sshd** (ADR-0039),
reached on a loopback-forwarded guest SSH port. This spec wires the one missing piece — the
forwarded SSH port — and flips the descriptor.

### What already exists on `main` (verified — do not rebuild)

1. **In-guest sshd + key.** `rootfs_build.py::_real_virt_builder` installs `openssh-server`, runs
   `systemctl enable sshd.service`, and `--ssh-inject root:file:<managed pubkey>` (ADR-0052). sshd
   is a normal `multi-user.target` unit; the readiness-marker unit is `WantedBy=multi-user.target`
   and fires on the live drive, so `multi-user.target` is reached under direct-kernel boot — sshd
   starts. **No boot-path change is needed.** The key is injected to **`root`**'s
   `authorized_keys`, so the live SSH transport connects as **`root@127.0.0.1`** with the managed
   private key as the identity (see §8 for the credential-ref / identity contract).
2. **Credential plumbing.** `sessions_lifecycle._resolve_credential` resolves the profile's
   `ssh_credential_ref` through the bound secret backend **before** `open_transport`, gated on
   `LocalLibvirtProfilePolicy.drgn_live_requires_credential` (returns `True`) +
   `ssh_credential_ref` (reads `profile.provider.local_libvirt.ssh_credential_ref`). The resolved
   value registers into the redaction registry by the backend's structural post-condition
   (ADR-0039 §2). The connect-plane `_open_ssh` orchestration (loopback-before-IO, ssh reachability
   probe, `ssh://` handle codec) is real and unit-tested (`test_connect.py`).
3. **The `_real_resolve_ssh_endpoint` / `_open_ssh` seams** exist in `connect.py`; only the
   resolver body is a stub.

### The gap this spec closes (verified against `main`)

The local domain (`render_domain_xml`) has **no network interface** — direct-kernel,
whole-disk-ext4, no `<interface>`/`-netdev` anywhere. So there is no host→guest SSH path and no
forwarded port for `_real_resolve_ssh_endpoint` to read. Provision must **render and record** the
forward before the resolver can read it.

## Decision

### 1. Profile signal: `ssh_credential_ref` is the drgn-live opt-in

A System realizes drgn-live over SSH iff its profile sets
`provider.local_libvirt.ssh_credential_ref` (the existing field; a profile that does not opt into
live SSH leaves it `None`, per the `LibvirtProfile` contract and ADR-0039 §2). The SSH forward is
rendered iff `ssh_credential_ref is not None`. This reuses the exact signal the session-layer
credential resolution already keys on, so the "provisioned for drgn-live" and "needs a credential"
predicates can never diverge.

### 2. Provision: render a loopback SSH forward + NIC and record the port

In `render_domain_xml`, when (and only when) `section.ssh_credential_ref is not None`, append to
the `<qemu:commandline>` passthrough (the same element the gdbstub `-gdb` arg uses; create it if
absent, append to it if the gdbstub arg already created it):

```xml
<qemu:arg value="-netdev"/>
<qemu:arg value="user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<port>-:22"/>
<qemu:arg value="-device"/>
<qemu:arg value="virtio-net-pci,netdev=kdivessh"/>
```

`-netdev user` is QEMU's built-in unprivileged SLIRP user-mode network (no bridge, no root, no
daemon). `hostfwd=tcp:127.0.0.1:<port>-:22` forwards the **loopback-only** host port to guest:22.
The `127.0.0.1` literal is hard-coded (single-host; the loopback bind is the security boundary,
mirrored by `_is_loopback_literal` at connect time). `restrict=on` **isolates the guest to the
forwarded port only**: it blocks every guest-initiated outbound packet (NAT'd internet/DNS, any
host-network access) the drgn-live control channel never needs, so an agent-supplied kernel cannot
use the new NIC for egress; the inbound `hostfwd` SSH connection and SLIRP's built-in DHCP still
work, so the guest still gets `10.0.2.15`. This is defense-in-depth on the only NIC drgn-live adds.

**Guest-side networking is a live-confirm obligation, not a proven fact.** The `rootfs_build`
plane installs and enables sshd but configures **no** guest networking — it relies on the fedora
`virt-builder` base template's default network stack (NetworkManager) bringing the single
virtio NIC up by DHCP. Whether that default suffices under direct-kernel boot is **not** provable
in CI (no booted guest) and is **not yet confirmed on hardware** (B6 has not run). The B6 live
drive must verify the guest acquires `10.0.2.15` and sshd is reachable on guest:22; if the base
template does not DHCP the NIC automatically, `rootfs_build` gains a one-line network-enable step
(a `systemctl enable systemd-networkd` + a DHCP `.network` drop-in, or a NetworkManager default
profile) as a follow-up. This spec does **not** assert the guest networks today; it renders the
host-side forward and records that the guest-side DHCP is the one remaining live-proof risk.

`render_domain_xml` gains keyword-only `ssh_port: int | None = None`. With `ssh_credential_ref`
set and `ssh_port=None` → `CONFIGURATION_ERROR` (programming error: the provisioner always
allocates when the flag is set). `ssh_port` is ignored when `ssh_credential_ref is None`.

Refactor so the gdbstub `-gdb` args and the SSH `-netdev`/`-device` args append to **one**
`<qemu:commandline>` element (`_qemu_commandline(domain)` returns the existing element or creates
one). A System with both gdbstub and a credential ref carries both arg sets in one element.

### 3. Port allocation: reuse B1's idempotent bind-probe + recorded-port-reuse

`provision()` computes `ssh_port` exactly as it computes `gdb_port`:

1. If the System's domain is already defined **and** records a forwarded SSH port, reuse it (read
   back via `recorded_ssh_port(domain.XMLDesc(0))`).
2. Otherwise call the injected `self._free_port()` bind-probe (the same seam gdbstub uses) for a
   fresh loopback port.

A `_ssh_port_for(system_id)` method mirrors `_gdb_port_for` (reuse-or-bind-probe over a
`_recorded_ssh_port(system_id)` lookup mirroring `_recorded_gdb_port`). Both run **only** when their
flag is set, so a System with neither opens no extra connection; a System with both opens the
existing-domain lookup connection for each (the lookups are independent and each closes its
connection). The bind-probe TOCTOU window is accepted exactly as for gdbstub (loopback,
single-host, single-attach; a collision is a clean domain-start `PROVISIONING_FAILURE` the
transactional `_define_and_start` handles).

### 4. Install preserves the SSH forward (no new edit)

`install._render_os_section` already calls `register_qemu_namespace()` before `ET.tostring` (the
B1 fix for the `<ns0:commandline>` prefix-strip hazard). The SSH args live in the **same**
`<qemu:commandline>` element, so that fix already preserves them. A regression test runs a
provision XML carrying the SSH forward through the install os-edit and asserts the `qemu:`-prefixed
`-netdev ... hostfwd ...` arg survives.

### 5. Resolve: read the recorded forwarded port back

`_real_resolve_ssh_endpoint(system)` becomes a real resolver mirroring `_resolve_endpoint_via`:
connect, `lookupByName(str(system))`, `XMLDesc()`, parse with `recorded_ssh_port`, return
`("127.0.0.1", port)`. Build it with a `_resolve_ssh_endpoint_via(connect)` factory so it is
unit-tested with a fake connection, and wire `from_env()` to
`_resolve_ssh_endpoint_via(_default_connect)`. The libvirt `open`/`XMLDesc` is the only
`live_vm` seam.

Error contract (mirrors the gdbstub resolver §3):
- Domain not found (`VIR_ERR_NO_DOMAIN`) → `CONFIGURATION_ERROR` ("System has no running libvirt
  domain to open a drgn-live SSH transport to").
- Domain exists, no recorded SSH port → `CONFIGURATION_ERROR` ("System was not provisioned for
  drgn-live; reprovision with `ssh_credential_ref` set"). **Not** `MISSING_DEPENDENCY`.
- Other libvirt error → `INFRASTRUCTURE_FAILURE`.
- Malformed XML → `INFRASTRUCTURE_FAILURE`.

`_real_ssh_connect` stays `live_vm`-gated (needs a booted guest + the resolved credential the
caller already registered).

### 6. Shared `recorded_ssh_port` reader

Add `recorded_ssh_port` / `recorded_ssh_port_from_root` to `providers/shared/libvirt_xml.py`
beside `recorded_gdb_port`. It walks the same `<qemu:arg>` list and, for the **`-netdev`** arg
value (the arg immediately following a `-netdev` arg, mirroring how `recorded_gdb_port` keys off
the arg after `-gdb`), extracts the port from the exact substring
`hostfwd=tcp:127.0.0.1:<port>-:22`. Parse contract, pinned to avoid ambiguity:

- It matches the kdive-rendered shape specifically: host literal `127.0.0.1`, guest port `22`,
  via a regex `hostfwd=tcp:127\.0\.0\.1:(\d+)-:22` anchored on those literals. A `-netdev` value
  with no matching `hostfwd` (or a different host/guest-port) yields `None` — the System was not
  provisioned with the kdive SSH forward.
- The first matching `-netdev` value wins. kdive renders exactly one SSH `-netdev` per domain, so
  "first match" is unambiguous; a hand-edited domain with multiple is out of contract (the parser
  reads the first, deterministically).
- Non-integer or absent port → `None`. A malformed-XML wrapper (`recorded_ssh_port(xml)`) returns
  `None` (the resolver maps that to `INFRASTRUCTURE_FAILURE` itself, as it does for the gdbstub
  reader — the bare parser never raises).

### 7. Descriptor + maturity

- `composition.py`: `supported_debug_transports = frozenset({"gdbstub", "drgn-live"})`. Single
  descriptor edit; the file is a cross-agent conflict zone, so the change is minimal/additive.
- `debug.*` tool maturity stays `partial` (ADR-0208 invariant 5: maturity asserts the plane works
  on hardware; CI proves only the fake-seam contract — no KVM, no booted sshd). B6 (#680) promotes
  maturity after the live drive. `supported_introspection` is **untouched** (`introspect.run`
  `live` mode stays unadvertised until B3 #677).

### 8. SSH user + identity contract (the live `_real_ssh_connect` seam)

The rootfs injects the managed public key to **`root`**, so the live transport connects as
**`root@127.0.0.1:<port>`**. The connecting identity is the **managed private key**
(`managed_private_key_path()`, ADR-0052) — the durable half of the keypair whose public half the
build injected. This is a hard pairing: the `ssh_credential_ref` an operator sets in the profile
**must resolve to that managed private key** for auth to succeed (the build injected only the
managed public key, so no other key is authorized). The profile carries `ssh_credential_ref` (the
secret-backend reference) rather than hard-wiring the managed path so the secret-by-reference +
redaction-registration contract (ADR-0039 §2) is uniform across providers and the operator
controls where the key bytes live; but the *value* it resolves to must be the managed key.

This pairing is a documented obligation, not a runtime-enforced check (the connector cannot
compare an opaque resolved secret to the managed key without holding both, defeating the
by-reference design). A mismatch surfaces as an SSH auth failure → `DEBUG_ATTACH_FAILURE` from
`_open_ssh` (the probe rejects an endpoint that does not accept a connection) — an honest,
actionable failure, not a silent wrong-credential success. `_real_ssh_connect` itself is
`live_vm`-gated and out of CI scope; the spec pins **user=`root`** and **identity=managed private
key** as its contract so B3 (#677), which builds the real drgn-over-SSH attach on top, inherits a
defined connect target rather than re-deriving it.

## Acceptance

**CI (fakes):**
- `render_domain_xml` renders the `-netdev user,...hostfwd=tcp:127.0.0.1:<port>-:22` + `virtio-net`
  args **iff** `ssh_credential_ref` is set, on `127.0.0.1`, with the allocated port; renders none
  when it is `None`.
- A System with **both** `debug.gdbstub` and `ssh_credential_ref` carries the `-gdb` arg **and**
  the `-netdev`/`hostfwd` args in **one** `<qemu:commandline>` element.
- `render_domain_xml(ssh_credential_ref set, ssh_port=None)` → `CONFIGURATION_ERROR`.
- SSH-port allocation reuses a recorded port (idempotent retry) and bind-probes a fresh one
  otherwise (mirrors the gdbstub allocation tests).
- The install os-edit preserves the SSH forward: a provision XML carrying the `hostfwd` arg
  survives `install._render_os_section` with the `qemu:` prefix and the `hostfwd=tcp:` arg intact.
- `recorded_ssh_port` reads the forwarded port from a rendered domain; returns `None` for a domain
  with no forward, a malformed-XML wrapper returns `None`.
- `_real_resolve_ssh_endpoint` (via `_resolve_ssh_endpoint_via`) returns `("127.0.0.1", port)` from
  a fake domain XML; maps not-found, no-recorded-port, and malformed-XML to the categories in §5.
- Loopback enforcement: `_open_ssh` rejects a non-loopback / hostname resolved host **before any
  IO** (the ssh_connect seam is never called) — already covered, re-asserted against the real
  resolver wiring.
- `supported_debug_transports == frozenset({"gdbstub", "drgn-live"})` in `build_runtime`; admission
  now admits `debug.start_session(..., "drgn-live")` on local and still rejects an unknown
  transport. `supported_introspection` unchanged.
- `debug.*` tool maturity remains `partial`.

**Live (KVM host), B6 #680 post-merge:** `debug.start_session(run, "drgn-live")` opens a real
loopback SSH transport into the booted guest's sshd; drgn attaches over it (B3 #677). Maturity
promotes only after this proof.

## Considered & rejected

(See ADR-0218 "Considered & rejected" — passt vs SLIRP, libvirt-native `<interface>` vs the qemu
passthrough, a stored port field vs the live domain XML, promoting maturity on green CI, wiring B3
here, a configured port range. Not re-argued here.)
