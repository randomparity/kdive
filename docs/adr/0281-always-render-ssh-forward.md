# ADR-0281: always render the local-libvirt SSH forward (decouple from `ssh_credential_ref`) (#937)

- Status: Accepted
- Date: 2026-06-30

## Context

ADR-0271 gave an agent direct SSH to a ready System through two tools — `systems.ssh_info`
(read the loopback coordinates) and `systems.authorize_ssh_key` (append the agent's public
key). Both only work if the System carries an SSH forward, and ADR-0218 renders that forward
**only** when the provisioning profile sets `provider.local_libvirt.ssh_credential_ref`. A
System provisioned without that field has no NIC and no forward, so both tools fail with
`CONFIGURATION_ERROR` and the only enable path is `systems.reprovision` — destructive. The
black-box review filed this as #937: getting a post-boot reproducer running, a routine
kernel-test task, costs a destructive reprovision or out-of-band console/image surgery.

ADR-0271 named this exact decoupling as separable follow-up scope (Consequences §4, and the
"Decouple the SSH forward from `ssh_credential_ref`" rejected alternative). This ADR is that
follow-up.

What the forward actually is, and what gates it (verified on `main`):

- The forward is plumbing: a QEMU user-mode NIC plus a loopback `hostfwd`
  (`-netdev user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<port>-:22` + a `virtio-net-pci`
  device), rendered by `_append_ssh_forward` in `providers/local_libvirt/lifecycle/xml.py`.
  `restrict=on` blocks all guest-initiated egress; only the inbound forwarded port works
  (ADR-0218 §2). The host side binds `127.0.0.1` on the worker host (loopback-only, ADR-0210).
- The host port is an OS-assigned ephemeral port (`_bind_probe_free_port` binds `127.0.0.1:0`
  and reads back the assignment), not a value drawn from a bounded pool.
- ~~The managed ed25519 key (ADR-0052) is injected into **every** guest's `root`
  `authorized_keys` at build time, regardless of `ssh_credential_ref`.~~ *Superseded by
  [ADR-0289](0289-per-system-ssh-bootstrap-key.md) — build-time injection is deleted;
  a per-System bootstrap public key is injected at provision instead.* The worker can
  still root-SSH any booted guest that has a reachable forward (with that System's key).

So `ssh_credential_ref` currently gates two unrelated things: (1) whether the forward/NIC are
rendered at all, and (2) the drgn-live introspection credential the debug-session path
resolves (`mcp/tools/debug/sessions_lifecycle.py`). Only (2) is a genuine credential concern;
(1) is plumbing that the managed-key SSH path (#782) and drgn-live both ride on.

## Decision

**Render the SSH forward on every local-libvirt provision, unconditionally.** Drop the
`ssh_credential_ref is not None` gate in both the renderer and the provisioner:

- `render_domain_xml` always appends `_append_ssh_forward`. `ssh_port` becomes a **required**
  input: a `None` is a `CONFIGURATION_ERROR`, mirroring how `kernel_path` is already required
  (a local domain must never render a half-configured forward, exactly as it must never
  disk-boot a bootloader-less rootfs).
- The provisioner always allocates the SSH port via `_ssh_port_for`, which still reuses the
  port recorded in an already-defined domain on an idempotent provision retry (unchanged), so
  the running QEMU's `hostfwd` and the resolver never diverge.

**`ssh_credential_ref` keeps only its drgn-live meaning.** The debug-session credential gate
in `sessions_lifecycle.py` is unchanged: drgn-live still requires the field to resolve its SSH
key. The field no longer gates plumbing — it is purely the introspection-transport credential
reference.

**Agent-surface effect.** `systems.ssh_info` and `systems.authorize_ssh_key` now succeed on any
ready local-libvirt System without a reprovision. The `Connector.recorded_ssh_endpoint` → `None`
branch — and the `_UNPROVISIONED_DETAIL` message behind it — now means only "this provider does
not expose a loopback SSH forward": remote-libvirt and fault-inject still return `None`, and a
local System defined before this change has no forward. The message drops the now-inapplicable
"reprovision with `ssh_credential_ref` set" remedy for local-libvirt, where the forward is
always present.

**Scope.** Local-libvirt only. No schema/migration, no new tool, no new parameter, no RBAC,
error-category, config, or destructive-op change. Remote-libvirt and fault-inject
`recorded_ssh_endpoint` behavior is untouched.

## Consequences

- The routine post-boot reproducer workflow no longer needs a destructive reprovision: on any
  ready local System an agent authorizes its key, reads the coordinates, and SSHes in.
- **Security posture.** Every ready local System now exposes one loopback-only, `restrict=on`,
  key-authenticated SSH port from boot — the same forward ADR-0218/0271 already accepted for
  drgn-live Systems, now universal. The guest stays loopback-only (ADR-0210), egress-blocked,
  and authenticated by the managed key already present; no off-host listener and no new stored
  secret. For throwaway debug VMs running agent-supplied kernels this is a bounded increase
  consistent with #782's stated threat model (development-only VMs, no real PII).
- **drgn-live is unchanged.** It still requires `ssh_credential_ref` (the credential gate
  upstream). It now always finds the forward present, but that gate decides whether the
  transport opens.
- The `_append_ssh_forward` `ssh_port is None` guard and the connect-resolver
  `recorded_ssh_port is None` config-error branch become unreachable for a correctly
  provisioned local System (the forward is always present), but stay as honest guards for
  remote/fault-inject and pre-change domains.
- **Resource cost.** One ephemeral loopback port and one NIC per System, OS-assigned, with no
  pool to exhaust — negligible.
- Test fakes that returned a constant `free_port` now hand the same value to both the gdbstub
  `-gdb` arg and the SSH `hostfwd` in a gdbstub provision — an impossible port collision they
  must stop modeling; the fakes move to distinct-per-call ports.

## Considered & rejected

- **Runtime hot-add "enable SSH" job** (libvirt attach-device + QMP `hostfwd_add` + trigger
  guest DHCP). Retrofits an already-running System without reboot, but depends on the guest
  cooperating to bring up a hot-plugged NIC and adds a `JobKind` + worker handler. Rejected:
  fragile for marginal benefit; always-render covers every System from boot.
- **Always-render plus a reboot retrofit** for Systems booted before this change. A reboot
  defeats an in-progress reproducer much like reprovision, for extra control-plane surface. The
  only gap always-render leaves — a System booted under the old code — is a transient
  one-release condition on throwaway VMs. Rejected.
- **Build the in-guest run-command facility here** (issue direction 2). Already owned by #909
  (open, `priority:high`, fully specified) and #910; building it here duplicates higher-priority
  scoped work and a raw exec returns no Run-correlated artifacts. Rejected; #937 is the
  forward-decouple slice only.
- **Add a separate `ssh_enabled` profile flag** instead of always-rendering. Two flags for one
  plumbing concern, and an agent would still have to pre-set it at provision — the same
  destructive-reprovision trap for an already-booted System. Rejected; the plumbing should just
  be present.
- **Drop `ssh_credential_ref` now that it no longer gates the forward.** It still gates the
  drgn-live introspection credential, a real secret reference resolved through the bound
  backend. Rejected; the field keeps that single remaining meaning.
- **A new `ErrorCategory` for "SSH forward absent".** `CONFIGURATION_ERROR` already models it,
  and the branch is now unreachable for local. Rejected per the stable-taxonomy invariant.
