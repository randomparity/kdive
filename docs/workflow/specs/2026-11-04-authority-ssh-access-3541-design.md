# Authority-owned Systems in the SSH access tools — design (#3541)

Amends [ADR-0811](../../adr/0811-authority-owned-system-ssh-forward-is-persisted-from-the-proof.md).
Plan: [2026-11-04-authority-ssh-access-3541](../plans/2026-11-04-authority-ssh-access-3541.md).

## Problem

`systems.ssh_info`, `systems.authorize_ssh_key`, `systems.check_ssh_reachable` and the two
connectivity job handlers call `Connector.recorded_ssh_endpoint`, which reads domain XML on the
process's own libvirt connection. An authority-owned local-libvirt System lives in the authority's
private daemon, so each fails with `system_domain_not_found`. #3536 fixed `debug.start_session` by
storing the port the authority reports; these five call sites still do not read it.

## Design

**The server resolves, the job carries, the connector builds.**

1. `Connector.recorded_ssh_endpoint(system, *, ssh_forward_port=None)` gains the keyword
   `open_transport` already has. The local connector returns `(127.0.0.1, port)` after
   `_checked_reported_port`, with no domain read. Every other connector ignores it.
2. `ssh_access.py` gets one private helper, `_recorded_endpoint(conn, resolver, system)`, returning
   `(endpoint, forward)`. It calls `authority_ssh_forward_port`, the only owner of the
   authority branch, and all three tools use it. No second policy is added.
3. `AuthorizeSshKeyPayload` and `CheckSshReachablePayload` gain
   `ssh_forward_port: SshForwardPort | None = None`, the strict 1024-65535 type the authority
   protocol defines. The tools set it to `forward`. `dump_payload` uses `exclude_none`, so a
   non-authority job's stored payload is byte-identical to today's.
4. Both handlers pass `payload.ssh_forward_port` to the connector. They do not read the binding:
   `resolve_authority_system_control_binding` is revoked from `kdive_worker` and stays revoked.
5. `authority_ssh_forward_port`'s refusal text stops saying "attach" and "reprovision", which an
   authority-owned System refuses; it says to tear down and provision again. Reasons do not change.
6. `ssh_info` suggests only `systems.get` while `ordinary_mutation_is_fenced` is true, because the
   two job tools refuse in that state. Otherwise its next actions are unchanged.
7. `authorize_ssh_key_handler` reads no System today. When the payload carries a port it now
   refuses with `reason=system_not_ready` unless the System is `ready`, the check
   `check_ssh_reachable_handler` already makes. The domain read was also a liveness check; a stored
   port is not, and a torn-down System's port can be reused.

No migration, no grant change, no new error reason.

### What each tool does

`system.state` must be `ready` at every tool, as today. Ownership state then decides:

| Ownership state | `ssh_info` | `check_ssh_reachable` | `authorize_ssh_key` |
|---|---|---|---|
| `ready` | stored port | refused: `authority_system_preactivation_mutation_fenced` | refused: same reason, by admission |
| `activated`, no restricting activation | stored port | job carries port | job carries port |
| `activated`, restricting activation | stored port | job carries port | refused: `external_boot_restricted` |
| any, port unreported | `authority_ssh_forward_unreported` | as row, then same reason | as row, then same reason |
| other states | `authority_system_not_attachable` | fenced | fenced |

Only `ssh_info` reaches the endpoint in `ready`. Lifting that fence is excluded. The two fences run
before the port read, so their reasons are unchanged. `ssh_info` has no fence, so `activated` with a
restricting activation reports the stored port, read-only, as a non-authority System does.

### Rolling deploy

Upgrade workers first, then servers. `_PayloadBase` forbids extra fields, so an old worker fails a
payload carrying the port with a payload validation error. Only an authority-owned job carries it,
and an old server never enqueues one for a local authority System, because its tool fails on the
domain read first. A new worker with an old server sees only port-free jobs and takes the domain
read, which is today's behaviour.

`LifecycleRequest` and `LifecycleResponse` reference no job payload model, so
`lifecycle_protocol_identity()` does not move and the native runner needs no refresh. The build
compares the identity before and after.

## Failure model

**Actors and deployments**

- Agents calling the three MCP tools through the authenticated server.
- The single-host systemd deployment: server, worker and local authority share a host and its
  loopback. A worker on another host cannot reach an authority forward.

**Invariants and assets at stake**

- The worker role never reads the authority binding.
- The endpoint host for a reported port is the constant loopback address; no stored value selects a
  host.
- Non-authority and remote-libvirt Systems: same connector read, same stored payload bytes.
- The System bootstrap key is offered only to a port that System reported while `ready`.

**Accepted failure classes**

- A job carrying the port meets an older worker, if servers upgrade first or the workers roll back.
  It fails with a payload validation error. Bounded to authority-owned Systems, which had no SSH
  path before this change. `check_ssh_reachable` reissues; `authorize_ssh_key` takes the replay
  class below.
- An identical key whose job already failed replays that job. That is the existing, documented
  `authorize_ssh_key` contract; authorize a different key.
- Between the handler's `ready` read and the probe the System can start teardown. Same window as
  `check_ssh_reachable_handler` today; a reused port answers with a guest that does not hold this
  System's bootstrap key, so authorization fails.

**Covered elsewhere**

- Remote-libvirt authority endpoint: #3547. Port immutability: #3546 (migration 0191).
- Preactivation fence and active-activation denial, NULL-port backfill, relocated fixture Systems
  with no binding: operator.

## Threat model

**Boundaries.** Widened: the job payload now carries a value from the authority's signed receipt
into the worker. Added: none.

**Actors.** An authenticated agent (VIEWER or CONTRIBUTOR) who names a `system_id` and cannot supply
a port. Trust sits in `kdive_server`, the only role that enqueues these jobs, and in the database
checks on the stored port from ADR-0811.

**Controls.** The tools take no port argument, so a caller cannot influence it; project and role
checks run first, unchanged. `SshForwardPort` bounds the value on enqueue and on `load_payload`
(strict: string, float and boolean refused). `_checked_reported_port` re-bounds it in the connector.
The host is a constant. A failure leaks only existing reason strings.

**Out of scope.** A role able to write `jobs` directly could already forge any job. The port is not
confidential: `ssh_info` discloses it to a VIEWER.

## Testing

Tool tests cover each table row, and non-authority plus remote-libvirt regressions. Handler tests
run on a real `kdive_worker` connection that is first shown to be refused the binding function, with
a real local connector whose domain read raises; the reachability test uses the real probe against a
local banner socket. Payload bounds and the connector branch get focused unit tests.

No planned arm logs in to a real authority guest as the worker: `tests/live_vm` and
`tests/live_stack` hold no SSH tool test, and a lab grant belongs to the campaign. The completion
report states that gap.
