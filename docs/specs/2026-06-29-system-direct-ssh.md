# Spec — Direct SSH to a System (agent-supplied key, #782)

- **Status:** Draft
- **Date:** 2026-06-29
- **Issue:** [#782](https://github.com/randomparity/kdive/issues/782) (status:needs-design)
- **ADR:** [ADR-0271](../adr/0271-system-direct-ssh-access.md) (this spec is its concrete
  realization). Builds on [ADR-0218](../adr/0218-local-libvirt-session-ssh-transport.md)
  (the loopback SSH forward), [ADR-0052](../adr/0052-bootable-rootfs-image-builder.md) (the
  managed keypair injected to guest `root`), [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md)
  §1 (loopback-only transports), and [ADR-0148](../adr/0148-rbac-scoped-tool-exposure.md)
  (RBAC-scoped tool exposure).

## Context

After a System is `ready` the agent cannot get a guest shell: the MCP surface exposes no
SSH coordinates, no credentials, and no exec path (black-box review, #782). This spec adds
two MCP tools that let an agent supply its own public key, have it authorized in the guest,
and read the connection coordinates — then SSH in with its own private key. KDIVE never
holds the agent's private key.

### What already exists on `main` (verified — do not rebuild)

1. **Loopback SSH forward.** A System provisioned with `provider.local_libvirt.ssh_credential_ref`
   set renders `-netdev user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<port>-:22` into
   its domain (ADR-0218); the host port is recorded in the domain XML and read by
   `recorded_ssh_port` / `recorded_ssh_port_from_root` (`providers/shared/libvirt_xml.py`).
2. **Managed key in guest `root`.** The build injects the managed ed25519 **public** key
   into guest `root`'s `authorized_keys` (ADR-0052). `managed_private_key_path()` is the
   matching private identity the worker uses to root-SSH the guest (see
   `providers/local_libvirt/debug/introspect.py`, which SSH-execs the live drgn helper as
   `root@127.0.0.1` with that identity).
3. **Worker SSH-exec seam.** The local introspect path already opens an SSH connection to
   the guest with a fixed argv and the managed identity. The authorize job reuses this
   shape (an injected SSH-exec callable) so it is unit-tested with a fake and `live_vm` is
   the only real-SSH gate.
4. **Tool/job registration + RBAC.** `mcp/tool_registration.py` (`PLANE_REGISTRARS`) and
   `mcp/worker_registration.py` register new tools/job handlers; `mcp/exposure.py`
   `_TOOL_SCOPES` classifies exposure; `security/authz/rbac.py` `require_role` enforces.
   `mcp/responses.py` `ToolResponse` is the uniform envelope; `ToolResponse.from_job`
   lifts a worker job into one.

### The gap this spec closes

No tool discloses the recorded SSH coordinates, and no tool authorizes an agent key in the
guest. The worker *can* reach the guest (managed key) but nothing drives it on the agent's
behalf for key authorization.

## Decision

### 1. Public-key validation (`security` helper)

A pure function `validate_authorized_public_key(raw: str) -> str` returns the normalized
single-line key or raises `CategorizedError(CONFIGURATION_ERROR, reason="invalid_public_key")`.
Rules (the trust boundary for a root-granting append):

- Exactly one non-empty line after strip; reject embedded `\n`/`\r` and any control char.
- First whitespace-delimited token ∈ `{ssh-ed25519, ssh-rsa, ecdsa-sha2-nistp256,
  ecdsa-sha2-nistp384, ecdsa-sha2-nistp521, sk-ssh-ed25519@openssh.com,
  sk-ecdsa-sha2-nistp256@openssh.com}`. A line beginning with anything else (notably an
  `authorized_keys` *options* field such as `command="..."`, `no-pty`, `environment=...`)
  is rejected — options are not accepted, so no forced-command/option smuggling.
- Second token is valid base64 (the key blob); reject if it does not decode.
- An optional third+ field (comment) is allowed but must contain no control chars.
- Total length bounded (e.g. ≤ 8 KiB) to bound the append.

Tested directly: accepts a real ed25519/rsa/ecdsa key; rejects empty, multi-line, a
`command=`-prefixed line, a bare blob with no type, a non-base64 blob, control chars, and
an over-length input.

### 2. `systems.ssh_info(system_id)` — read-only, synchronous, VIEWER

Handler (in the systems plane) follows the `systems.get` shape: `current_context()`,
`SYSTEMS.get`, project-visibility + `require_role(ctx, project, Role.VIEWER)`. Then:

- If `system.state` is not `READY` → `ToolResponse.failure(..., READINESS_FAILURE)`.
- Resolve the recorded SSH port from the live domain XML via the connect/runtime seam
  (`recorded_ssh_port`). If absent → `ToolResponse.failure(..., CONFIGURATION_ERROR,
  reason="ssh_not_provisioned")` with detail "System was not provisioned for SSH;
  reprovision with `ssh_credential_ref` set."
- Success: `ToolResponse.success(object_id=system_id, data={"ssh": {"user": "root",
  "host": "127.0.0.1", "port": <int>, "jump_host": None}}, suggested_next_actions=
  ["systems.authorize_ssh_key", "systems.get"])`. `port` is a native JSON int (ADR-0263).

Reading the recorded port needs the live domain. Expose it through the existing local
connect/runtime port (a small `recorded_ssh_endpoint(system) -> (host, port) | None` seam
mirroring `_resolve_ssh_endpoint_via`) so the handler is unit-tested with a fake and the
libvirt `open`/`XMLDesc` is the only `live_vm` dependency. `jump_host` is hard `None` for
local-libvirt.

### 3. `systems.authorize_ssh_key(system_id, public_key)` — mutating, worker job, OPERATOR

- Tool handler: `require_role(ctx, project, Role.OPERATOR)`; validate the System is
  `ready` and SSH-provisioned (same checks as §2, returning the same categories) **before**
  enqueueing — fail fast without spending a job; then validate the public key (§1) at the
  tool boundary so a malformed key is a synchronous `CONFIGURATION_ERROR`, never a job that
  fails later. Enqueue a new `JobKind` (`authorize_ssh_key`) carrying `system_id` +
  normalized key; return `ToolResponse.from_job(...)` (`{job_id, status: running}`).
- Worker handler (registered in `worker_registration.py`): resolve the recorded SSH
  endpoint, open an SSH connection as `root@127.0.0.1:<port>` with
  `managed_private_key_path()`, and run a fixed-argv idempotent append:
  `ssh ... root@host -- /bin/sh -c '<append-if-absent>'`. The append uses a here-doc-free
  fixed command: create `~/.ssh` `0700` if absent, then append the key to
  `authorized_keys` `0600` only if an exact-line match is not already present (idempotent
  re-authorize is a no-op). The key is passed as a fixed argv element, never interpolated
  into a shell string.
- Job result → envelope: success `data={"authorized": true, "system_id": ...}`,
  `suggested_next_actions=["systems.ssh_info"]`. SSH failure → `TRANSPORT_FAILURE`; a
  guest that rejects the managed identity or a missing endpoint → `CONFIGURATION_ERROR`.
- The append command and the SSH-exec are an injected seam (default = the real managed-key
  SSH-exec), so the worker handler is unit-tested with a fake that records argv and
  simulates present/absent key, success/failure — no `live_vm` needed for the unit path.

### 4. Registration, exposure, no migration

- `tool_registration.py`: append the two systems tools to the systems registrar (they need
  only `pool` + the runtime/connect seam already injected into that plane).
- `worker_registration.py`: register the `authorize_ssh_key` job handler.
- `exposure.py` `_TOOL_SCOPES`: `"systems.ssh_info": VIEWER`, `"systems.authorize_ssh_key":
  OPERATOR`. The completeness guard (`CLASSIFIED_TOOLS | PUBLIC_TOOLS` == live registry)
  forces both to be classified.
- `JobKind` gains `authorize_ssh_key`. **No DB migration** — nothing is persisted; the job
  payload is transient and the authorized key lives only in the guest. (If the project's
  `JobKind` is a DB-checked enum requiring a migration, add the minimal additive migration;
  confirm during implementation. Current reading: `JobKind` is an in-code enum and the
  jobs table `kind` check was last widened by migration 0051 for the composite — verify
  whether a new kind needs a `jobs_kind_check` widening and, if so, add migration 0052.)
- Regenerate the committed tool reference (`just docs`).

### 5. Acceptance

**CI (fakes, no KVM):**
- `validate_authorized_public_key` accepts valid ed25519/rsa/ecdsa keys; rejects empty,
  multi-line, `command=`-prefixed, optionless-but-typeless, non-base64, control-char, and
  over-length inputs.
- `systems.ssh_info`: not-ready → `READINESS_FAILURE`; no recorded port →
  `CONFIGURATION_ERROR` (`reason=ssh_not_provisioned`); ready + port → success with
  `data.ssh == {user: root, host: 127.0.0.1, port: <int>, jump_host: null}` and a native
  int port.
- `systems.authorize_ssh_key` tool: VIEWER caller denied (RBAC); non-ready →
  `READINESS_FAILURE`; malformed key → synchronous `CONFIGURATION_ERROR`; happy path →
  `from_job` running envelope and the enqueued job carries the normalized key.
- `authorize_ssh_key` worker handler (fake SSH-exec): builds `root@127.0.0.1:<port>` with
  the managed identity; append is idempotent (key already present → no second append);
  SSH failure → `TRANSPORT_FAILURE`; missing endpoint → `CONFIGURATION_ERROR`. The key is
  a fixed argv element (asserted), never shell-interpolated.
- Exposure: both tools classified; completeness guard green. `just docs` regenerated.

**Live (`live_vm`, this KVM host):** provision a System with `ssh_credential_ref` set;
`systems.authorize_ssh_key` with a freshly generated test pubkey; `systems.ssh_info`
returns the port; `ssh -i <test privkey> -p <port> root@127.0.0.1 true` succeeds. Proves
the end-to-end agent-supplied-key path on real hardware.

## Considered & rejected

See ADR-0271 "Considered & rejected" (server-mediated exec holding a private key, raw-key
return, a KDIVE-brokered bastion now, off-loopback rebind, a provision-time key param,
decoupling the forward from `ssh_credential_ref`, metadata-channel injection, a persisted
key ledger, a new error category). Not re-argued here.
