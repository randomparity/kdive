# ADR-0271: agent-supplied-key direct SSH access to a System (#782)

- Status: Accepted
- Date: 2026-06-29

## Context

After a System reaches `ready` an agent has no way to get a shell in the guest. The
black-box review recorded the gap concretely: the MCP surface exposes no usable SSH
coordinates, no credentials, and no guest command-execution path. `systems.get` returns
an allowlisted provisioning summary (`provisioning_profile_summary` — arch, boot method,
sizing) and deliberately omits every SSH detail. The drgn-live live-attach path reaches
the guest over SSH, but only as a provider-internal transport that the operator must
pre-arm by setting `provider.local_libvirt.ssh_credential_ref` in the profile; it is not
an agent-driveable connection.

What already exists on `main` (the machinery this builds on, verified — not rebuilt):

- A System provisioned with `ssh_credential_ref` set renders a loopback SSH forward into
  its libvirt domain: `-netdev user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<port>-:22`
  plus a virtio NIC (ADR-0218). `restrict=on` isolates the guest to that inbound forward
  (no guest-initiated egress). The forwarded host port is recorded in the domain XML and
  read back by `recorded_ssh_port` (`providers/shared/libvirt_xml.py`).
- The build injects kdive's **managed** ed25519 public key (ADR-0052) into the guest
  `root` account's `authorized_keys` (`--ssh-inject root:file:<managed pubkey>`). The
  matching private key is `managed_private_key_path()`. So the **worker can already
  root-SSH into any SSH-enabled guest** with the managed identity — `introspect.py`
  does exactly this for the live drgn helper.
- The forward binds `127.0.0.1` on the **worker host** and is loopback-only by design
  (ADR-0210): the guest never listens off-host. A process on the worker host can reach
  `127.0.0.1:<port>`; a remote process cannot, except by tunnelling through something on
  the worker host (an SSH ProxyJump bastion).

The reachability constraint is the crux. "Direct SSH" can mean (a) the server runs
commands in the guest on the agent's behalf and returns output, or (b) the agent's own
SSH client connects to the guest. (b) is the literal request, keeps the private key out
of KDIVE, and is the cloud pattern (user-supplied key injected at launch); the cloud
milestone needs a jump-host model regardless. The issue itself flags the meta-questions:
does this collapse other tools, and how provider-dependent is it.

## Decision

**Agent supplies a public key; KDIVE never holds the agent's private key.** Realize
direct SSH as two new MCP tools over the existing local-libvirt SSH forward.

**1. `systems.authorize_ssh_key(system_id, public_key)` — mutating, OPERATOR, worker job.**
The agent generates its keypair locally and passes only the **public** key. A new worker
job connects to the guest over the loopback forward using the managed private key
(`managed_private_key_path()`, the same identity `introspect` uses) and idempotently
appends the supplied key to `/root/.ssh/authorized_keys`. The supplied value is a public
key — not a secret — so it is not redacted, but it is **strictly validated before use**:
exactly one line, a key-type token from a fixed allow-list
(`ssh-ed25519`/`ssh-rsa`/`ecdsa-sha2-nistp{256,384,521}`/`sk-*`), base64 blob, optional
comment, no control characters, no embedded newline, no leading `command=`/options field.
That validation is the security boundary — it prevents an `authorized_keys` injection
that smuggles a forced-command, options, or extra authorized lines. Append is idempotent
(a key already present is not duplicated). The job returns the standard
`{job_id, status: running}`; the agent polls `jobs.*`.

**2. `systems.ssh_info(system_id)` — read-only, VIEWER, synchronous.** Returns the
provider-agnostic connection descriptor for a `ready` System, derived from the recorded
domain XML (no guest contact):

```json
{ "user": "root", "host": "127.0.0.1", "port": 22022, "jump_host": null }
```

`jump_host` is modelled now and emitted `null` for local-libvirt (the agent is co-located
with the worker on the single-host deployment). The field exists so the cloud milestone
populates it with a bastion (`{host, port, user}`) and the agent connects with
`ssh -J <bastion> ...` without a contract change. ADR-0210 is preserved: the guest stays
loopback-only; remote reach is a future ProxyJump *through* the worker/bastion, never an
off-host guest listener.

**3. Preconditions and errors (existing taxonomy, no new category).**

- System not `ready` → `READINESS_FAILURE`.
- System has no recorded SSH forward (not provisioned with `ssh_credential_ref`) →
  `CONFIGURATION_ERROR` with the actionable detail "reprovision with `ssh_credential_ref`
  set", mirroring the drgn-live resolver's own message.
- Malformed/disallowed public key → `CONFIGURATION_ERROR` (`reason=invalid_public_key`),
  validated in the service layer (not a FastMCP `Field` bound, which would leak a raw
  `ValidationError` through `BindingErrorMiddleware`, per ADR-0247/0259/0264).
- Worker cannot reach the guest sshd → `TRANSPORT_FAILURE`.
- RBAC denial is handled by `require_role` + the denial-audit middleware (ADR-0148); the
  tools are classified in `_TOOL_SCOPES` (`ssh_info` VIEWER, `authorize_ssh_key`
  OPERATOR).

**4. Scope: local-libvirt only; no schema migration.** The connection descriptor is
derived from the live domain XML and the append is an in-guest mutation, so nothing is
persisted — no migration. Remote-libvirt (authorize via the guest agent) and cloud
(routable host + populated `jump_host`) are follow-ups that fill the same two-tool
contract.

## Consequences

- An agent gets a real, self-driven SSH session: it authorizes its own key, reads the
  coordinates, and runs `ssh -i <its key> -p <port> root@127.0.0.1` (co-located) or, on a
  future cloud System, `ssh -J <bastion> ...`. KDIVE stores and redacts nothing new — the
  agent's private key never enters the system.
- **Collapses tooling:** for the agent's own access the manual profile-level
  `ssh_credential_ref` pre-provisioning is no longer the gate — the agent self-authorizes
  per System. drgn-live gains a credentialed path that does not depend on an operator
  wiring a secret reference ahead of time. The introspect/debug-session tools are
  complemented, not replaced.
- **Provider dependence:** the tool *surface* is provider-agnostic; only the *realization*
  (managed-key SSH append over the loopback forward, loopback coordinates) is
  local-libvirt-specific. The N-provider seam is the descriptor's `jump_host` field and
  the per-provider authorize realization.
- A System must still carry the SSH forward (provisioned with `ssh_credential_ref`) for
  either tool to do anything — until a follow-up decouples forward-rendering from the
  credential ref, the precondition is an honest `CONFIGURATION_ERROR`, not a silent
  no-op.
- The `authorized_keys` validator is the trust boundary for a root-granting operation; it
  is unit-tested adversarially (forced-command, options, multi-line, control chars).

## Considered & rejected

- **Server-mediated `systems.exec` that runs commands and returns output (KDIVE holds a
  generated per-System private key).** Works for a fully-remote agent against
  local-libvirt today (the server does the SSH), but it moves a private key into KDIVE —
  the exact secret surface the redaction system exists to avoid — and gives only exec, not
  a real session (no interactive shell, scp, port-forward, or agent-driven drgn). Rejected
  in favour of agent-supplied keys; an exec convenience can be layered later if a no-SSH
  client ever needs it.
- **Return the agent's private key / a generated key over MCP.** Returning private key
  bytes fights the secret-by-reference + redaction model wholesale. Rejected; the agent
  keeps its private key.
- **Build a KDIVE-brokered tunnel / worker SSH bastion now** so a remote agent reaches the
  local loopback forward. Substantial new networked surface (a TCP relay into a root sshd)
  for a single-host milestone whose agent is co-located. Rejected for this PR; the
  `jump_host` field models the bastion so the cloud milestone implements it once, for the
  case that actually needs it.
- **Rebind the forward to `0.0.0.0` + firewall.** Violates ADR-0210's loopback-only
  invariant and exposes the guest on the network. Rejected.
- **Accept the public key as a `systems.provision` parameter (apply at ready).** Larger
  surface and a deferred-apply edge case for marginal convenience. Rejected; a standalone
  authorize tool on a ready System is the minimal surface.
- **Decouple the SSH forward from `ssh_credential_ref` in this PR** so every local System
  is SSH-reachable without the drgn-live opt-in. Reopens ADR-0218's gating and risks the
  drgn-live credential-resolution path. Rejected as separable follow-up scope; flagged in
  Consequences.
- **Inject the key via cloud-init / fw_cfg / a metadata channel / image rebuild.** The
  worker already has managed-key root SSH into the booted guest, so a runtime append is
  strictly simpler and needs no boot-path or image change. Rejected the heavier channels.
- **Persist authorized keys in a new column (migration).** The in-guest `authorized_keys`
  is the record; a throwaway debug VM needs no durable key ledger. Rejected; no migration.
- **Add a new `ErrorCategory` for "SSH not available".** `READINESS_FAILURE` /
  `CONFIGURATION_ERROR` / `TRANSPORT_FAILURE` already model the failure modes. Rejected per
  the stable-taxonomy invariant.
