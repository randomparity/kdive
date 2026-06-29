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
  "host": "127.0.0.1", "port": <int>, "jump_host": None, "host_scope":
  "worker_loopback"}}, suggested_next_actions=visible_next_actions(
  ["systems.authorize_ssh_key", "systems.get"], ctx, system.project))`. `port` is a native
  JSON int (ADR-0263).
  The next-actions are built through the ADR-0261 `visible_next_actions` role filter, so a
  VIEWER caller (who cannot invoke the OPERATOR-only `systems.authorize_ssh_key`) is not
  pointed at it — only the actions its role can actually call survive.

`host_scope` is the **locality signal** the descriptor carries so a caller can tell whether
the coordinates are usable from where it runs. `worker_loopback` means `host` is the worker
host's loopback (ADR-0210) — reachable only by a process co-located with the worker, or via
a `jump_host` once populated. A remote agent that reads `worker_loopback` with `jump_host:
null` knows it must run on the worker host (today's single-host deployment) or wait for the
bastion (cloud milestone), rather than silently dialing its own `127.0.0.1`. The cloud
milestone emits `host_scope: "routable"` with a populated `jump_host`.

Reading the recorded port needs the live domain. This is **synchronous and server-side** —
the established pattern: `debug.start_session` is itself synchronous (no `JobKind`,
`sessions_lifecycle.py`) and resolves its transport endpoint by reading the domain XML
through the local connect seam (`_resolve_ssh_endpoint_via(_default_connect)`). `ssh_info`
reuses that exact seam (a small `recorded_ssh_endpoint(system) -> (host, port) | None`
mirroring `_resolve_ssh_endpoint_via`), so the handler is unit-tested with a fake and the
libvirt `open`/`XMLDesc` is the only `live_vm` dependency. The port is **not** persisted to
the DB — ADR-0218 settled the live-domain-XML-vs-stored-port choice in favour of the domain
XML, and duplicating it would risk drift. `jump_host` is hard `None` for local-libvirt.

### 3. `systems.authorize_ssh_key(system_id, public_key)` — mutating, worker job, OPERATOR

- Tool handler: `require_role(ctx, project, Role.OPERATOR)`; validate the System is
  `ready` and SSH-provisioned (same checks as §2, returning the same categories) **before**
  enqueueing — fail fast without spending a job; then validate the public key (§1) at the
  tool boundary so a malformed key is a synchronous `CONFIGURATION_ERROR`, never a job that
  fails later. Enqueue a new `JobKind` (`authorize_ssh_key`) carrying `system_id` +
  normalized key; return `ToolResponse.from_job(...)`. The job `dedup_key` is
  `{system_id}:authorize_ssh_key:{sha256(normalized_key)[:16]}` — it **includes the key
  fingerprint** so re-authorizing the *same* key is idempotent (the `dedup_key` UNIQUE column
  returns the prior job) while a *distinct* key gets its own job; a System-only dedup_key
  would silently collapse every key after the first into the first job.
- Worker handler (registered in `worker_registration.py`): resolve the recorded SSH
  endpoint, open an SSH connection as `root@127.0.0.1:<port>` with
  `managed_private_key_path()`, and run a fixed remote append script. The **key is delivered
  on the SSH session's stdin**, never in the command string: `ssh host CMD` space-joins any
  post-host argv into one string the remote login shell re-parses, so an argv-positioned key
  would not be isolated and the remote shell would interpret comment-field metacharacters.
  The worker therefore passes exactly **one** post-host argument (the script) and pipes the
  validated key via `subprocess.run(input=key)`; the script reads it with `key=$(cat)`. The
  script `umask 077`s (so `~/.ssh` is `0700` and a new `authorized_keys` is `0600`), takes a
  `flock -w 5` on a dedicated lock FD so concurrent authorize jobs cannot interleave the
  read-modify-write, and appends the key only if an exact-line match (`grep -qxF "$key"`) is
  absent (re-authorizing the same key is a no-op). There is no command-string position for
  the key to break out of.
- **Readiness / timeout.** A System can report `ready` (serial marker on
  `multi-user.target`) before the SSH-NIC has finished DHCP and sshd is reachable — #697
  flagged guest-side SSH-NIC DHCP as a live-confirm obligation, not a proven invariant. The
  job therefore opens the connection with a bounded SSH connect timeout (a fixed module
  constant, e.g. `ConnectTimeout=10`) and maps an unreachable/timed-out sshd to
  `TRANSPORT_FAILURE`, which the envelope marks **retryable** (`_RETRYABLE_BY_CATEGORY`), so
  the agent re-issues `authorize_ssh_key` after the guest finishes coming up. The job does
  no internal retry loop (the queue/agent owns retry); it fails fast within the timeout.
- Job result → envelope: success `data={"authorized": true, "system_id": ...}`,
  `suggested_next_actions=["systems.ssh_info"]`. Unreachable/timed-out/refused sshd →
  `TRANSPORT_FAILURE` (retryable); a missing recorded endpoint or a guest that rejects the
  managed identity → `CONFIGURATION_ERROR` (not retryable — a reprovision/config fault).
- The append command and the SSH-exec are an injected seam (default = the real managed-key
  SSH-exec), so the worker handler is unit-tested with a fake that records argv and
  simulates present/absent key, success/failure — no `live_vm` needed for the unit path.

### 4. Registration, exposure, migration 0052

- `tool_registration.py`: append the two systems tools to the systems registrar (they need
  only `pool` + the runtime/connect seam already injected into that plane).
- `worker_registration.py`: register the `authorize_ssh_key` job handler.
- `exposure.py` `_TOOL_SCOPES`: `"systems.ssh_info": VIEWER`, `"systems.authorize_ssh_key":
  OPERATOR`. The completeness guard (`CLASSIFIED_TOOLS | PUBLIC_TOOLS` == live registry)
  forces both to be classified.
- `JobKind` gains `authorize_ssh_key`. **Migration 0052** (additive, forward-only) widens
  the `jobs_kind_check` CHECK to admit `'authorize_ssh_key'`, drop-and-recreating the
  constraint to keep its name stable for the SQL↔enum tie — exactly as migration 0051 did
  for `build_install_boot`. This is the *only* schema change: no new table or column. The
  authorized key lives only in the guest (no key ledger) and the job payload is transient,
  but the `JobKind` enum value must be admitted by the DB CHECK or the insert fails.
- Regenerate the committed tool reference (`just docs`).

### 5. Acceptance

**CI (fakes, no KVM):**
- `validate_authorized_public_key` accepts valid ed25519/rsa/ecdsa keys; rejects empty,
  multi-line, `command=`-prefixed, optionless-but-typeless, non-base64, control-char, and
  over-length inputs.
- `systems.ssh_info`: not-ready → `READINESS_FAILURE`; no recorded port →
  `CONFIGURATION_ERROR` (`reason=ssh_not_provisioned`); ready + port → success with
  `data.ssh == {user: root, host: 127.0.0.1, port: <int>, jump_host: null, host_scope:
  worker_loopback}` and a native int port. An OPERATOR caller's success
  `suggested_next_actions` includes `systems.authorize_ssh_key`; a VIEWER caller's omits it
  (ADR-0261 `visible_next_actions` role filter).
- `systems.authorize_ssh_key` tool: VIEWER caller denied (RBAC); non-ready →
  `READINESS_FAILURE`; malformed key → synchronous `CONFIGURATION_ERROR`; happy path →
  `from_job` running envelope and the enqueued job carries the normalized key.
- `authorize_ssh_key` worker handler (fake SSH-exec): builds `root@127.0.0.1:<port>` with
  the managed identity and a bounded `ConnectTimeout`; the append command is `flock`-guarded
  and idempotent (key already present → no second append); unreachable/timed-out sshd →
  `TRANSPORT_FAILURE` (retryable in the envelope); missing endpoint → `CONFIGURATION_ERROR`
  (not retryable). The key is **absent from the argv** (asserted) and delivered on stdin, and
  two distinct keys on one System enqueue two distinct jobs while the same key re-authorizes
  to the same job (dedup_key fingerprint).
- Migration 0052 applies forward and admits a `kind='authorize_ssh_key'` jobs insert; the
  schema test suite (which replays migrations) stays green.
- Exposure: both tools classified; completeness guard green. `just docs` regenerated.

**Prerequisite (live):** the live acceptance inherits #697's open guest-DHCP risk — it
passes only once the SSH-NIC reliably DHCPs and sshd is reachable on the booted guest. If
that is not yet confirmed on this host, the CI (fake-seam) acceptance still holds and the
live proof is gated on that confirmation.

**Live (`live_vm`, this KVM host):** provision a System with `ssh_credential_ref` set;
`systems.authorize_ssh_key` with a freshly generated test pubkey; `systems.ssh_info`
returns the port; `ssh -i <test privkey> -p <port> root@127.0.0.1 true` succeeds. Proves
the end-to-end agent-supplied-key path on real hardware.

## Considered & rejected

See ADR-0271 "Considered & rejected" (server-mediated exec holding a private key, raw-key
return, a KDIVE-brokered bastion now, off-loopback rebind, a provision-time key param,
decoupling the forward from `ssh_credential_ref`, metadata-channel injection, a persisted
key ledger, a new error category). Not re-argued here.
