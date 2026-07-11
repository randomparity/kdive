# ADR 0291 — Remote-libvirt SSH bootstrap-key injection + agent SSH parity

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers
- **Follows:** ADR-0289 (per-System bootstrap key; named remote injection as a follow-up)
- **Revises:** ADR-0271 (`authorize_ssh_key` targets the recorded endpoint host, not a
  hardcoded loopback), ADR-0078 (a bounded `/bin/sh` exception for provisioning-time key
  injection, in the spirit of the ADR-0100 build-VM exception).

## Context

ADR-0289 gave every System a unique per-System SSH bootstrap keypair and injected the
public half into the guest, but the injection is local-libvirt-only: it runs
`virt-customize --ssh-inject` against the overlay on the worker's own host through the
provision-time overlay-customizer seam. Remote-libvirt discards the customizers
(`del overlay_customizers`) because a remote System's disk lives on a remote libvirt host
the worker cannot run local `virt-customize` against. So a remote guest never receives
the bootstrap key, and `systems.ssh_info` / `authorize_ssh_key` are unavailable on remote
Systems (`recorded_ssh_endpoint` → `None`) — remote exposes no reachable SSH endpoint
like local's loopback `hostfwd`.

Two capabilities are missing and separable: (1) placing the key **into** the remote
guest, and (2) a **reachable SSH endpoint** so the worker (for `authorize_ssh_key`) and
the agent can SSH in. The only pre-SSH channel to a remote guest is the qemu-guest-agent,
which remote already speaks (drgn-live, build, kdump). This ADR delivers full SSH parity.
See `docs/archive/superpowers/specs/2026-07-01-remote-ssh-key-injection-design.md`.

## Decision

Deliver both capabilities, activated **only** when the operator declares SSH config on
the `[[remote_libvirt]]` instance — so the new off-host network exposure is a conscious
opt-in and, absent it, remote behaves exactly as today (no key injected — no consumer
would exist — and `authorize_ssh_key` still rejects).

- **Config-gated activation.** Add optional `ssh_addr` (the ACL'd bind address, sibling
  of `gdb_addr`) and `ssh_range` (`"min:max"`, sibling of `gdbstub_range`, every port
  assignable) to the instance and `RemoteLibvirtConfig`. Both set → SSH parity active;
  both unset → inactive (unchanged behavior); exactly one set → `CONFIGURATION_ERROR`
  (a half-configured forward is an operator error, fail-closed at op time).
- **Reachable endpoint via user-mode `hostfwd`.** When active, `render_domain_xml`
  appends `-netdev user,restrict=on,hostfwd=tcp:<ssh_addr>:<ssh_port>-:22` +
  `virtio-net-pci` to `<qemu:commandline>`, with `ssh_port` allocated per-System by
  enumerating the ports recorded in defined `kdive-` domains (the ADR-0080 gdbstub
  registry pattern: atomic with `defineXML`, freed by `undefine`, read over TLS). This
  mirrors local's loopback forward (ADR-0218), differing only in the routable ACL'd bind
  address. `recorded_ssh_endpoint` returns `(ssh_addr, ssh_port)`, so the provider-agnostic
  `ssh_info`/`authorize_ssh_key` tools light up with no tool change.
- **Bootstrap-key injection over the guest agent.** A `RemoteBootstrapKeyInjector` writes
  the public key into `/root/.ssh/authorized_keys` via one fixed, worker-composed
  `/bin/sh -c` `guest-exec` hop (allowlist `{"/bin/sh"}`, key on **stdin** — no injection
  surface), running after `wait_for_agent` inside `provision`/`reprovision`. The script is
  the ADR-0271 `authorize_ssh_key` shape (`umask 077; mkdir -p; grep -qxF || append`), so
  it is idempotent on retry and needs no guest sshd. The `systems` handler already ensures
  the key (committed) for every provider; a new `bootstrap_pubkey` param on the
  `Provisioner` port carries the ensured public key to the provider — local ignores it
  (keeps its pre-boot overlay-customizer injection), remote uses it (post-boot,
  guest-agent). Two carriers coexist because the injection **phases** differ (overlay file
  before boot vs. running guest after agent-ready) and cannot share a signature.
- **`authorize_ssh_key` over the endpoint host.** Generalize `build_authorize_argv` from
  a hardcoded `127.0.0.1` to the recorded endpoint host (local stays loopback; remote is
  `ssh_addr`). The worker loads the bootstrap **private** key and SSHes to the endpoint to
  append the agent's key — the genuine consumer that makes the injected key load-bearing.

## Consequences

- Remote reaches SSH parity with local: bootstrap key present in-guest, `ssh_info`
  returns a reachable endpoint, `authorize_ssh_key` succeeds, and an agent can SSH in with
  its own key — all provider-agnostically through the existing tools.
- No migration (the `system_bootstrap_keys` table and `authorize_ssh_key` job kind exist;
  `ssh_port` lives in domain XML, `ssh_addr` in `systems.toml`). Teardown key reclaim is
  already provider-agnostic and unchanged.
- New off-host network exposure: QEMU binds `ssh_addr:ssh_port`. `restrict=on` isolates
  the slirp NIC to inbound-forward-only; the operator ACL on `ssh_addr:ssh_range` is the
  security boundary (the same trust model as `gdb_addr`). Operators must ACL the range;
  the worker must be able to reach it (it already reaches the host over qemu+tls).
- A bounded exception to ADR-0078's debug-target no-shell rule: one fixed, worker-composed
  `/bin/sh -c` write with the only variable on stdin. Precedent: ADR-0100 (build VM),
  and `ssh_authorize` already runs an equivalent script over SSH against the debug guest.
  The guest-agent exec allowlist stays worker-side and single-program.
- The SSH forward adds a second (slirp) NIC the guest must DHCP; a guest that does not
  bring it up is unreachable. This is the primary live-proof risk (cf. the prior
  debian-DHCP defect); the kdive-ready images' cloud-init NIC bring-up (ADR-0288) is
  expected to cover it and the `live_vm` two-host proof validates it.
- kdive still custodies only its own ephemeral per-System infra key (ADR-0289); the agent
  key is never held. The stdin-delivered key never appears in argv, the command string, or
  a captured transcript.
- Host-key trust changes relative to local. `authorize_ssh_key` keeps
  `StrictHostKeyChecking=no`, which is harmless on local's unspoofable loopback but, on
  remote's routable path to `ssh_addr`, means the worker does not verify the guest sshd's
  identity. Pubkey auth never discloses the bootstrap private key, so the residual risk is
  a path-local impostor accepting the agent's (non-secret) public key and reporting a
  success the real guest never received — a functional failure, not credential theft.
  This is the same "operator ACL on the bind address is the security boundary" model the
  remote gdbstub already relies on (RSP has no auth), so it is **accepted here, mitigated
  by the operator ACL on `ssh_addr:ssh_range`**. Host-key pinning via a guest-agent-read
  `known_hosts` is a named future hardening, deferred to avoid a guest-agent round-trip on
  every authorize.

## Considered & rejected

- **Expose the guest's bridge DHCP IP** (guest-agent `guest-network-get-interfaces`) as
  the endpoint. No second NIC, but the endpoint is not recorded in domain XML (a live
  query per call), reachability depends on bridge routability, and it breaks the
  "endpoint recorded in XML, read over TLS" invariant Connect relies on.
- **`virt-customize --ssh-inject` over the remote connection.** The ADR-0289 obstacle: no
  worker-side libguestfs access to a remote disk without a host-access channel kdive
  deliberately lacks.
- **cloud-init NoCloud seed** (reuse #962). ADR-0289's foothold caveat: the bootstrap
  foothold must not depend on first-boot config succeeding.
- **A baked `kdive-authorize-key` helper** (respects the no-shell rule) — adds a
  build-time image contract across all images and cannot live-prove without rebuilding
  every image; disproportionate to a fixed one-line write.
- **Required `ssh_addr`/`ssh_range`** (like `gdb_addr`). Forces off-host SSH exposure on
  every remote deployment and churns every config/fixture; the config-gated optional
  design keeps it opt-in.
- **Replacing the `overlay_customizers` seam with a single `bootstrap_pubkey`.** Cleaner
  long-term but re-opens the just-merged ADR-0289 seam and touches local's injection path
  and tests; out of scope. The two carriers coexist because the phases differ.
