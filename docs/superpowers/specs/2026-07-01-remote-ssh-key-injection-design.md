# Remote-libvirt SSH bootstrap-key injection + agent SSH parity (#966)

- **Status:** Draft
- **Issue:** #966
- **ADR:** [ADR-0291](../../adr/0291-remote-ssh-bootstrap-injection.md)
- **Related:** ADR-0289 (per-System bootstrap key), ADR-0271 (`authorize_ssh_key`),
  ADR-0218 (drgn-live transport), ADR-0281 (always-render local SSH forward),
  ADR-0080 (remote provisioning / gdbstub port registry), ADR-0078/0100 (guest-agent
  exec seam + the build-VM `/bin/sh` exception), #962/ADR-0288 (cloud-init first boot).

## 1. Problem

ADR-0289 gave every System a unique per-System SSH bootstrap keypair generated at
provision and injected into the guest. The key **service** (`system_bootstrap_keys`
table + `ensure_/load_/delete_system_bootstrap_key`) is provider-agnostic, but the
**injection** is local-libvirt-only: it runs `virt-customize --ssh-inject` against the
overlay on the worker's own host through the provision-time overlay-customizer seam.

Remote-libvirt discards the customizers (`provisioning.py:175` — `del
overlay_customizers`). A remote guest therefore never receives the bootstrap key, and
because remote exposes no reachable SSH endpoint, `systems.ssh_info` /
`systems.authorize_ssh_key` return "not provisioned" for remote Systems
(`recorded_ssh_endpoint` → `None`; `connect.py:108`).

The ADR-0289 obstacle: a remote System's disk lives on a remote libvirt host, so the
worker cannot run local `virt-customize` against it.

Two capabilities are missing, and they are separable:

1. **Getting the bootstrap key *into* the remote guest.** Achievable — the only pre-SSH
   channel to a remote guest is the qemu-guest-agent, which remote already speaks
   (drgn-live, build, kdump all ride `guest-exec`).
2. **A reachable SSH endpoint** so an agent (and the worker, for `authorize_ssh_key`)
   can SSH into a remote System, at parity with local's loopback `hostfwd`. Remote's
   guest is on another host with no such forward.

This spec delivers **full SSH parity**: both capabilities.

## 2. Goals / non-goals

**Goals**

- A remote System (on a host configured for SSH parity) has its per-System bootstrap
  public key in the guest `/root/.ssh/authorized_keys` after provision.
- `systems.ssh_info` returns a reachable endpoint for such a System.
- `systems.authorize_ssh_key` succeeds for such a System; an agent can then SSH in with
  its own key.
- The key row is reclaimed at teardown (already provider-agnostic; unchanged).
- Live-provable on the two-host remote-libvirt HW setup behind the `live_vm` gate.

**Non-goals**

- No change to local-libvirt injection, its overlay-customizer seam, or its tests.
- No DB migration (the `system_bootstrap_keys` table and the `authorize_ssh_key` job
  kind already exist; the SSH port lives in domain XML, `ssh_addr` in `systems.toml`).
- No change to remote guest-agent introspection (drgn-live stays guest-agent-based).
- No cloud-init dependency for the foothold (ADR-0289's caveat: the bootstrap foothold
  must not depend on first-boot config succeeding).

## 3. Activation: config-gated capability

The SSH-parity capability activates **iff** the operator declares both new optional
fields on the `[[remote_libvirt]]` instance:

- `ssh_addr` — the ACL'd host address QEMU binds the SSH forward to (the sibling of
  `gdb_addr`; the operator's ACL is the security boundary, per ADR-0079).
- `ssh_range` — `"min:max"` port range for per-System SSH forwards (sibling of
  `gdbstub_range`; every port in the range is assignable — no reserved probe port,
  unlike the gdbstub ACL-probe reservation).

When **either** is unset, remote-libvirt behaves exactly as today: no SSH forward NIC is
rendered, no bootstrap key is injected (no consumer exists, so injecting one would be a
phantom write), `recorded_ssh_endpoint` returns `None`, and `authorize_ssh_key` rejects
the System with the existing `ssh_not_provisioned` error. This keeps the new off-host
network exposure a conscious operator opt-in and leaves every existing inventory and
test path unchanged.

Both fields are optional on `RemoteLibvirtInstance` and `RemoteLibvirtConfig`. Config
resolution raises `CONFIGURATION_ERROR`, fail-closed at op time (the same posture as the
gdbstub range), when:

- exactly one of `ssh_addr` / `ssh_range` is set (a half-configured forward);
- `ssh_range` is malformed / inverted / out of `1..65535`;
- `ssh_addr == gdb_addr` **and** the `ssh_range` overlaps `gdbstub_range` — the two
  forwards would then contend for the same host socket namespace, so the ranges must be
  disjoint on a shared bind address. (Distinct addresses may reuse a port number.)

## 4. Design

### 4.1 SSH endpoint: user-mode `hostfwd` on the ACL'd `ssh_addr`

Mirror the gdbstub port-registry pattern (ADR-0080) exactly. When SSH parity is active,
`render_domain_xml` appends a QEMU user-mode NIC to `<qemu:commandline>`:

```
-netdev user,id=kdivessh,restrict=on,hostfwd=tcp:<ssh_addr>:<ssh_port>-:22
-device virtio-net-pci,netdev=kdivessh
```

- `restrict=on` isolates the slirp NIC (no guest-initiated outbound on it); `hostfwd`
  still forwards inbound `ssh_addr:ssh_port` → guest `:22`. This mirrors local's
  loopback `restrict=on` forward (ADR-0218), differing only in the bind address
  (routable ACL'd `ssh_addr` vs `127.0.0.1`).
- This is a **second** NIC alongside the existing bridge `<interface type="network">`;
  the guest must bring it up via DHCP (slirp serves `10.0.2.x`). sshd binds `0.0.0.0:22`
  by default, so the forward reaches it regardless of which NIC. **This is the primary
  live-proof risk** (a guest that does not DHCP the second NIC is unreachable — see the
  prior debian-DHCP defect in project memory); the `live_vm` proof is where it is
  validated, and the kdive-ready images' cloud-init NIC bring-up (ADR-0288) is expected
  to cover it.

**Per-System `ssh_port` allocation.** `allocate_ssh_port` and `used_ssh_ports` mirror
`allocate_gdb_port` / `used_gdb_ports`: enumerate the ports recorded in the defined
`kdive-` domains' XML and pick a free one in `[ssh_port_min, ssh_port_max]`. The record
is atomic with `defineXML`, freed by `undefine`, and read back over TLS. A new
`recorded_ssh_port_from_root` / `recorded_ssh_port` parses the `hostfwd` arg from a
domain's `<qemu:commandline>` (sibling of `recorded_gdb_port_from_root`).

The define/start retry loop (`_define_and_start`) already advances the gdbstub port on a
bounded set of start failures (a squatted port / define-start race, indistinguishable
from other faults at the libvirt layer). It now allocates **both** the gdb and ssh ports
per attempt and advances **both** candidates on failure (each into its own `tried` set),
keeping the same `_START_ATTEMPTS` bound. A within-range collision is handled by the
enumerate-used logic. The gdb and ssh ranges must not overlap **when `ssh_addr ==
gdb_addr`** (the same socket namespace) — config resolution rejects that with a
`CONFIGURATION_ERROR` naming both ranges (§3). When the addresses differ, an identical
port number is two distinct sockets and is allowed.

### 4.2 Connect plane: `recorded_ssh_endpoint`

`RemoteLibvirtConnect.recorded_ssh_endpoint(system)` returns `(ssh_addr, ssh_port)` when
SSH parity is active, else `None` (unchanged behavior). `ssh_addr` comes from config;
`ssh_port` is read from the domain's live XML **on the worker** — this must be a concrete
production read, **not** a `live_vm`-gated stub. Note that the gdbstub Connect path's
default `_real_resolve_port` (`connect.py:113`) *raises* `MISSING_DEPENDENCY` and
`from_env` injects no reader, so remote gdbstub port resolution is effectively injected
only under the `live_vm` gate. `recorded_ssh_endpoint` **must not** copy that shape: it is
called by the live worker's `authorize_ssh_key_handler` and by the `ssh_info` tool, so it
opens the TLS connection, calls `XMLDesc`, and parses via `recorded_ssh_port_strict`
directly (the connection open is the only live seam, injected for unit tests). A
service-level test asserts it returns the port — not `MISSING_DEPENDENCY` — against a fake
connection. No RSP probe (that is gdbstub-specific): `ssh_info` returns the recorded
endpoint **without** probing, exactly as local-libvirt does (ADR-0281) — reachability is
proven by the `authorize_ssh_key` SSH itself. The consequence for remote (a guest that has
not brought up the slirp NIC yields a syntactically-valid but dead endpoint) is the
second-NIC risk of §4.1, validated by the live-proof, not papered over with a provision
gate (see §7).

`systems.ssh_info` (VIEWER) and `systems.authorize_ssh_key` (OPERATOR) are already
provider-agnostic — they read `recorded_ssh_endpoint` — so they light up for remote with
no tool/exposure change.

### 4.3 Bootstrap-key injection over the guest agent

A new collaborator `RemoteBootstrapKeyInjector` writes the bootstrap public key into the
guest via a single fixed, worker-composed `/bin/sh -c` `guest-exec` hop, allowlist
`{"/bin/sh"}`, with the **key delivered on stdin** (never in argv or the command string —
no injection surface). The script is the same shape as `ssh_authorize._REMOTE_SCRIPT`
(ADR-0271):

```
set -e
umask 077
mkdir -p /root/.ssh
key=$(cat)
touch /root/.ssh/authorized_keys
grep -qxF "$key" /root/.ssh/authorized_keys \
  || printf '%s\n' "$key" >> /root/.ssh/authorized_keys
```

- `umask 077` gives `/root/.ssh` `0700` and a fresh `authorized_keys` `0600`.
- `grep -qxF ... ||` makes the append idempotent (a provision retry that reuses the
  overlay re-runs injection harmlessly; no duplicate lines).
- Runs **after** `wait_for_agent` inside remote `provision`/`reprovision`, reusing the
  connection and domain handle the provisioner already holds. It does **not** need the
  guest sshd to be up (it writes over the agent channel, not SSH).
- Failure raises through `GuestAgentExec`'s existing error contract
  (`CONFIGURATION_ERROR` / `TRANSPORT_FAILURE` / `INFRASTRUCTURE_FAILURE`) or a
  `PROVISIONING_FAILURE` on a non-zero script exit; provision fails and leaves the domain
  in place (diagnosable), consistent with the agent-gate failure posture (ADR-0080 §4).

This is a bounded divergence from ADR-0078's debug-target no-shell rule, documented in
ADR-0291. Precedent: ADR-0100 already allows a `/bin/sh` hop for the ephemeral build VM,
and `ssh_authorize` already runs an equivalent script (over SSH) against the debug guest.
The command is fixed and worker-composed; the only variable (the key) rides stdin.

The injector is an injected seam on `RemoteLibvirtProvisioning` (default = the real
guest-agent injector; unit tests pass a recording fake), so provisioning stays
unit-testable with no libvirt host.

### 4.4 Threading the public key to the provider

The `systems` provision/reprovision handlers already **ensure** the bootstrap key for
every provider (`_bootstrap_key_customizers` runs `ensure_system_bootstrap_key` in its
own committed transaction) but drop the pubkey for remote (`bootstrap_key_customizer is
None` → `()`), keeping the ADR-0289 commit-before-overlay ordering.

Add `bootstrap_pubkey: str | None = None` to the `Provisioner.provision` /
`reprovision` port. The handler passes the ensured pubkey as `bootstrap_pubkey`
alongside the existing `overlay_customizers`:

- **local-libvirt** ignores `bootstrap_pubkey` (its injection stays the pre-boot
  overlay-customizer path — unchanged).
- **remote-libvirt** ignores `overlay_customizers` (as today) and uses `bootstrap_pubkey`
  for the post-boot guest-agent injection — but only when SSH parity is active. When
  `bootstrap_pubkey` is `None` (a System that predates the key, defensive) or SSH parity
  is inactive, remote injects nothing.
- **fault-inject** ignores both (unchanged).

Two carriers coexist because the two injection **phases** genuinely differ: local mutates
the overlay **file before boot** (a `Callable[[str], None]` over an overlay path); remote
writes the **running guest after the agent is up** (needs the pubkey string plus the live
connection — no overlay path exists on the worker). They cannot share one signature. This
is additive and does not touch local's injection path or ADR-0289's seam.

### 4.5 `authorize_ssh_key` over the recorded endpoint host

`ssh_authorize.build_authorize_argv` hardcodes `127.0.0.1`. Generalize it to take the
endpoint **host**; the handler already resolves `endpoint = recorded_ssh_endpoint(...)`
and currently discards the host (`_host, port = endpoint`). Use the host:

- local: host is `127.0.0.1` — behavior unchanged.
- remote: host is `ssh_addr` — the worker loads the bootstrap **private** key, SSHes to
  `(ssh_addr, ssh_port)`, and appends the agent's key. This is the genuine consumer of
  the injected bootstrap key and makes the parity real.

**Host-key trust (a real change from local).** local's SSH runs over `127.0.0.1`, which
cannot be spoofed, so `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` are
harmless there. On remote the worker→guest SSH traverses a routable path to `ssh_addr`,
where a host on that path could impersonate the guest sshd. Pubkey auth never discloses
the bootstrap **private** key (the signature is session-bound), so an impostor cannot
steal the key; the residual exposure is that an impostor could accept the agent's
**public** key (not secret) and return success while the real guest never receives it —
`authorize_ssh_key` reports success against an impostor and the agent's later SSH to the
real endpoint fails. This is the same "operator ACL on the bind address is the security
boundary" trust model the remote gdbstub already relies on (ADR-0079; RSP has no auth
either), so it is an **accepted, ACL-mitigated risk documented in ADR-0291** rather than a
blocker. Host-key pinning (read `/etc/ssh/ssh_host_*.pub` via the already-open guest-agent
channel and hand the worker a `known_hosts`) is named as a future hardening, not
implemented here, to avoid coupling every authorize to a guest-agent round-trip; the
security-review step re-evaluates this.

**Reachability + retry.** The worker must be able to reach `ssh_addr:ssh_port`; it already
reaches the remote host over qemu+tls, and the operator ACL governs the port. The existing
connection-refused retry (`_AUTHORIZE_SSH_RETRY`) covers a freshly-`ready` guest whose sshd
is still binding — but remote's path is slower (slirp DHCP + route + sshd bind) than
local's ~46 ms, so the implementation reviews the retry window's total budget against the
remote path and states the chosen bound (rather than silently reusing the local default).

## 5. Files touched (indicative)

- `src/kdive/inventory/model.py` — optional `ssh_addr` / `ssh_range` on
  `RemoteLibvirtInstance`.
- `src/kdive/providers/remote_libvirt/config.py` — resolve/validate the fields
  (`_parse_ssh_range`, half-configured guard); `RemoteLibvirtConfig` gains
  `ssh_addr`/`ssh_port_min`/`ssh_port_max`.
- `src/kdive/providers/remote_libvirt/lifecycle/xml.py` — render the `hostfwd` NIC;
  `recorded_ssh_port(_from_root)`.
- `src/kdive/providers/remote_libvirt/lifecycle/gdb.py` (or a sibling) —
  `allocate_ssh_port` / `used_ssh_ports`.
- `src/kdive/providers/remote_libvirt/lifecycle/provisioning.py` — allocate the ssh
  port, render it, inject the key after `wait_for_agent`; injector seam.
- `src/kdive/providers/remote_libvirt/guest/bootstrap_key.py` — new
  `RemoteBootstrapKeyInjector`.
- `src/kdive/providers/remote_libvirt/lifecycle/connect.py` — `recorded_ssh_endpoint`.
- `src/kdive/providers/remote_libvirt/composition.py` — wire the injector default.
- `src/kdive/providers/ports/lifecycle.py` — `bootstrap_pubkey` on the `Provisioner`
  port.
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`,
  `providers/fault_inject/...` — accept + ignore `bootstrap_pubkey`.
- `src/kdive/jobs/handlers/systems.py` — pass `bootstrap_pubkey` to provision/reprovision.
- `src/kdive/jobs/handlers/ssh_authorize.py` — `build_authorize_argv(host, port, key)`.
- Docs: `systems.toml` example(s) + operator note on ACLing `ssh_addr:ssh_range`.

## 6. Testing

**Unit / service (run locally, CI-gated):**

- Config: `ssh_addr`+`ssh_range` parse into a config; malformed/inverted range →
  `CONFIGURATION_ERROR`; half-configured (one set, one unset) → `CONFIGURATION_ERROR`;
  both unset → SSH-parity inactive.
- XML: `hostfwd` NIC rendered with the right `ssh_addr:ssh_port-:22` and `restrict=on`
  when active; absent when inactive; `recorded_ssh_port` round-trips; a domain without
  the NIC → `None`.
- Port allocation: `used_ssh_ports` enumerates defined domains; `allocate_ssh_port`
  skips used + excluded ports; range exhaustion path.
- Provisioning: injector invoked after `wait_for_agent` with the right pubkey; not
  invoked when parity inactive or `bootstrap_pubkey` is `None`; injector failure fails
  provision and leaves the domain; both ports advance on a start-failure retry.
- Injector: composes `[/bin/sh, -c, <script>]` with the key on stdin; idempotent-append
  script shape; non-zero exit → raise; error categories from `GuestAgentExec`.
- Connect: `recorded_ssh_endpoint` returns `(ssh_addr, ssh_port)` when active, `None`
  otherwise — and returns the port (not `MISSING_DEPENDENCY`) against a fake TLS
  connection, proving it is a real production read, not a `live_vm` stub.
- Config: `ssh_addr == gdb_addr` with overlapping `ssh_range`/`gdbstub_range` →
  `CONFIGURATION_ERROR`; distinct addresses with the same port number → accepted.
- Handler: `bootstrap_pubkey` threaded to provision/reprovision; local ignores it;
  key ensured before provision (existing invariant preserved).
- `authorize_ssh_key`: `build_authorize_argv` uses the endpoint host; local argv
  unchanged (regression); remote argv targets `ssh_addr`.

**`live_vm` (two-host remote HW, gated):**

- Provision a remote System with SSH parity configured → assert the bootstrap public key
  is present in the guest `/root/.ssh/authorized_keys` (read via guest-agent) →
  `authorize_ssh_key` with an agent key → agent SSHes in with its own key → teardown →
  assert the `system_bootstrap_keys` row is gone.

This suite cannot be driven from the implementing session (no two-host remote HW
available here); it is implemented gated for an operator to run, and the PR states the
limitation. The closest local equivalent that does run is the full unit/service suite
above.

## 7. Considered & rejected

- **Expose the guest's bridge DHCP IP** (discover via guest-agent
  `guest-network-get-interfaces`) instead of a user-mode `hostfwd`. No second NIC, but
  the endpoint is not recorded in domain XML (a live guest-agent query per call),
  reachability depends on the bridge network being routable to worker + agent, and it
  breaks the "endpoint recorded in XML, read over TLS" invariant the Connect plane relies
  on. Rejected for architectural inconsistency with the gdbstub design.
- **A provision-time SSH reachability gate** (worker SSHes with the bootstrap key as a
  readiness check so a dead slirp NIC fails provision). Rejected: local declares a System
  `ready` on its boot marker **before** the guest sshd binds (ADR-0289 consequences) and
  does **not** gate provision on SSH reachability; adding such a gate to remote would
  diverge from local, couple provision success to sshd/DHCP timing, and make provision
  flakier — the opposite of parity. The dead-NIC risk is instead owned by the `live_vm`
  proof (which is precisely there to validate the image brings up the forward NIC), and
  `ssh_info` returns the recorded endpoint unprobed exactly as local does (§4.2).
- **`virt-customize --ssh-inject` over the remote libvirt connection.** The ADR-0289
  obstacle: the worker cannot run libguestfs/virt-customize against a disk on a remote
  host without a host-access channel kdive deliberately does not have (it speaks only
  libvirt TLS + guest-agent). Rejected.
- **cloud-init NoCloud seed** (reuse #962). ADR-0289's foothold caveat: the bootstrap
  foothold must not depend on first-boot config succeeding. Rejected.
- **A baked `kdive-authorize-key` helper** run via guest-exec (respects ADR-0078's
  no-shell rule). Adds a build-time image contract across all images and cannot
  live-prove without rebuilding every image — disproportionate to a fixed, worker-composed
  one-line write. Rejected in favor of the `/bin/sh` hop with a documented ADR exception.
- **Making `ssh_addr`/`ssh_range` required** (like `gdb_addr`). Forces the new off-host
  SSH exposure on every remote deployment and churns every existing config/fixture.
  Rejected for the config-gated optional design (§3).
- **Replacing the `overlay_customizers` seam with a single `bootstrap_pubkey`.** Cleaner
  long-term but re-opens the just-merged ADR-0289 seam design and touches local's
  injection path and tests. Rejected as out of scope; the two carriers coexist because
  the injection phases differ (§4.4).
