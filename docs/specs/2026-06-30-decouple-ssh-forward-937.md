# Spec: always render the local-libvirt SSH forward (decouple from `ssh_credential_ref`) (#937)

- Issue: #937
- ADR: [ADR-0281](../adr/0281-always-render-ssh-forward.md)
- Status: Draft

## Problem

There is no lightweight path to SSH into — or run a one-shot command on — a ready System. ADR-0271
gave an agent two tools, `systems.ssh_info` and `systems.authorize_ssh_key`, but both require the
System to carry an SSH forward, and ADR-0218 renders that forward only when the provisioning profile
sets `provider.local_libvirt.ssh_credential_ref`. A System provisioned without that field has no NIC
and no forward; both tools fail with `CONFIGURATION_ERROR` ("reprovision with `ssh_credential_ref`
set"), and the only enable path is `systems.reprovision` — destructive. Running a post-boot
reproducer, a routine kernel-test task, therefore costs a destructive reprovision or out-of-band
console/image surgery.

ADR-0271 flagged the fix as separable follow-up scope (its Consequences §4 and the rejected
alternative "Decouple the SSH forward from `ssh_credential_ref`"). This is that follow-up.

Three facts shape the gap:

- **The forward is plumbing, not a credential.** `_append_ssh_forward`
  (`providers/local_libvirt/lifecycle/xml.py`) renders a user-mode NIC plus a loopback
  `hostfwd=tcp:127.0.0.1:<port>-:22` with `restrict=on` (no guest egress; ADR-0218 §2). It is gated
  on `ssh_credential_ref` at `xml.py` (the `if section.ssh_credential_ref is not None` branch) and at
  `provisioning.py` (`ssh_port = self._ssh_port_for(...) if section.ssh_credential_ref is not None
  else None`).
- **The key is already there.** The managed ed25519 key (ADR-0052) is injected into every guest's
  `root` `authorized_keys` at build time, regardless of `ssh_credential_ref`. The worker can already
  root-SSH any booted guest with a reachable forward.
- **The port is not pooled.** `_bind_probe_free_port` binds `127.0.0.1:0` and reads back an
  OS-assigned ephemeral port, so always allocating one costs nothing scarce.

So `ssh_credential_ref` gates two unrelated things: (1) whether the forward/NIC exist, and (2) the
drgn-live introspection credential the debug-session path resolves
(`mcp/tools/debug/sessions_lifecycle.py`). Only (2) is a genuine credential concern.

## Goal

1. **Always render the forward.** Every local-libvirt provision renders the NIC + loopback
   `hostfwd`, regardless of `ssh_credential_ref`. `render_domain_xml` always appends
   `_append_ssh_forward`; `ssh_port` becomes required (a `None` is `CONFIGURATION_ERROR`, mirroring
   `kernel_path`). The provisioner always allocates the port via `_ssh_port_for`, keeping the
   reuse-on-idempotent-retry behavior.
2. **Keep `ssh_credential_ref` as the drgn-live credential only.** The debug-session credential gate
   is unchanged; the field no longer gates plumbing.
3. **Make the agent surface honest.** `systems.ssh_info`/`authorize_ssh_key` succeed on any ready
   local System. The `recorded_ssh_endpoint` → `None` branch (and `_UNPROVISIONED_DETAIL`) now means
   "this provider exposes no loopback SSH forward" (remote/fault-inject, or a pre-change domain); the
   message drops the inapplicable "reprovision with `ssh_credential_ref`" remedy for local.

## Non-goals

- **No in-guest run-command facility.** Issue direction 2 ("run a one-shot command") is owned by #909
  (open, `priority:high`) and #910 (Run-owned reproducer records). This spec is the forward-decouple
  slice only.
- **No runtime retrofit of already-booted Systems.** A System booted under the old code has no
  forward; bringing it online without a reboot (libvirt attach-device + QMP `hostfwd_add` + guest
  DHCP) is fragile and out of scope. Always-render covers every System from boot; the gap is a
  transient one-release condition on throwaway VMs. Note one subtlety in the idempotent-retry path:
  `provision()` calls `defineXML` (which rewrites the recorded XML to include the forward) then
  `create()`, and `create()` on an already-running domain returns `OPERATION_INVALID` and is treated
  as a no-op (`test_provision_already_running_domain_is_idempotent`). So if `provision()` is
  re-invoked against a still-running pre-change domain, the recorded XML would claim a forward the
  live QEMU lacks (`_recorded_ssh_port` reuse only helps a post-change domain that already recorded a
  port). This is bounded: the supported enable path for an existing System is the **destructive**
  `systems.reprovision` (teardown + fresh `create`, which does render the forward) — not a define-only
  retry against a healthy running System, which is not a normal flow.
- **No schema/migration, new tool/param, RBAC, error-category, or config change.** Local-libvirt only.

## Design

### Renderer (`xml.py`)

- Replace `if section.ssh_credential_ref is not None: _append_ssh_forward(domain, ssh_port)` with an
  unconditional `_append_ssh_forward(domain, ssh_port)`.
- `_append_ssh_forward` already raises `CONFIGURATION_ERROR` when `ssh_port is None`; the message
  ("a drgn-live System (`ssh_credential_ref` set) requires an allocated SSH port") is reworded to the
  new contract: a local-libvirt domain always renders the SSH forward and so requires an allocated
  SSH port. The `kernel_path`-style "required argument" framing is the model.
- Update the `render_domain_xml` docstring: the forward is rendered unconditionally; `ssh_port` is
  required (not "ignored unless `ssh_credential_ref` set"). `ssh_credential_ref`'s remaining role is
  the drgn-live credential.

### Provisioner (`provisioning.py`)

- Replace the conditional with an unconditional `ssh_port = self._ssh_port_for(system_id)`.
- `_ssh_port_for` / `_recorded_ssh_port` are unchanged: reuse the recorded port on an idempotent
  re-provision so the running QEMU's `hostfwd` and the resolver never diverge.

### Agent surface (`mcp/tools/lifecycle/systems/ssh_access.py`)

- Reword `_UNPROVISIONED_DETAIL` so it no longer prescribes a reprovision for local. It now describes
  the only remaining cause: the System's provider does not expose a loopback SSH forward (direct SSH
  to a System is a local-libvirt capability). The `data={"reason": "ssh_not_provisioned"}`
  discriminator stays for compatibility.

### drgn-live resolver message (`connect.py`)

- `_resolved_ssh_port`'s `port is None` config-error message still references "reprovision with the
  profile's `ssh_credential_ref` set". For local this branch is now unreachable (the forward is
  always present); it remains an honest guard. The drgn-live precondition the agent actually hits is
  the upstream credential gate in `sessions_lifecycle.py`, which is unchanged. Leave the resolver
  message as-is (it is a deep transport guard, not the agent-facing precondition) — noted here so a
  reviewer does not read its survival as an oversight.

## Test plan

Renderer (`tests/providers/local_libvirt/test_provisioning.py`):

- The negative tests flip. `test_render_omits_ssh_forward_when_no_credential_ref` and
  `test_render_ignores_ssh_port_when_no_credential_ref` are replaced by a test asserting the forward
  **is** rendered for a default profile (no credential ref) when `ssh_port` is supplied:
  `recorded_ssh_port(xml) == <port>` and the full `-netdev`/`-device` arg list.
- `test_render_rejects_credential_ref_without_an_ssh_port` becomes a generic
  `test_render_rejects_missing_ssh_port` (no credential ref needed): any render with `ssh_port=None`
  raises `CONFIGURATION_ERROR`.
- The `_render()` test helper gains an `ssh_port` default so the many `_render()`-based tests (kernel,
  preserve-on-crash, gdbstub) keep rendering a valid domain; their non-SSH assertions are unaffected
  by the added forward.
- The credential-ref-set and gdbstub+ssh coexistence tests stay (the forward still works with the
  credential ref present).

Provisioner (same file):

- `test_provision_non_ssh_does_not_allocate_an_ssh_port` flips to
  `test_provision_always_allocates_an_ssh_port`: a default-profile provision records an SSH port.
- The constant-`free_port` fakes (`lambda: 5555`, `lambda: 40022`) move to a distinct-per-call
  counter so a gdbstub provision — which now allocates **both** a gdb port and an ssh port — does not
  hand the same value to `-gdb tcp:127.0.0.1:<p>` and `hostfwd=...:<p>-:22` (a real collision). Tests
  that assert specific recorded ports assert against the counter's sequence.

Agent surface (`tests/mcp/lifecycle/test_systems_ssh_access.py`):

- The `recorded_ssh_endpoint` → `None` path still returns the `CONFIGURATION_ERROR` /
  `reason=ssh_not_provisioned` envelope (the fake connector returns `None`, modeling
  remote/fault-inject); the assertion updates to the reworded detail text. The success paths
  (`ssh_info` returns coordinates, `authorize_ssh_key` enqueues) are unchanged.

Connect (`tests/providers/local_libvirt/test_connect.py`):

- No behavior change; the `recorded_ssh_endpoint` and `_resolved_ssh_port` tests stand. Confirm they
  still pass (the resolver is untouched).

## Risks

- **Security posture.** Every ready local System now exposes a loopback-only, `restrict=on`,
  key-authenticated SSH port from boot — the forward ADR-0218/0271 already accepted for drgn-live
  Systems, now universal. Loopback-only (ADR-0210), egress-blocked, managed-key authenticated, no new
  stored secret. Acceptable for throwaway debug VMs per #782's threat model.
- **Failure surface shifts from synchronous to async for guest-network-down Systems.** Today a
  no-forward System fails synchronously at `ssh_info`/`authorize_ssh_key` with `CONFIGURATION_ERROR`
  (`reason=ssh_not_provisioned`) — a clear, self-describing signal. After always-render,
  `recorded_ssh_endpoint` returns coordinates for any ready local System, so `ssh_info` always
  succeeds and `authorize_ssh_key` always enqueues a worker job; the reachability failure for a System
  whose guest NIC is not up (the pre-existing #697 condition) now surfaces only as an asynchronous
  worker `TRANSPORT_FAILURE` the agent sees after polling `jobs.*`. This grows the
  "forward present but sshd unreachable" population from credential-ref Systems to all Systems. It is
  accepted: the forward is plumbing and guest-NIC reachability is a separate, pre-existing gap (#697),
  not a regression introduced here. `ssh_info` reads the recorded XML and never contacts the guest, so
  it cannot diagnose reachability; the worker `TRANSPORT_FAILURE` remains the honest signal.
- **Hidden gdbstub/ssh port collision in tests.** Surfaced above; the counter-fake fix is mandatory,
  not cosmetic — a constant fake would assert a domain that cannot start.
- **Already-booted Systems.** Out of scope by design; documented as a transient condition.
