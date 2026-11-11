# SSH endpoint for a remote-libvirt authority System (#3547)

Decision record: [ADR-0815](../../adr/0815-remote-authority-system-ssh-port-is-reported-by-the-authority.md).

## Scope and authority

Campaign scope for issue #3547, token `q3547-cbcd412f`. This change is the design only: this spec
and ADR-0815. The operator decided on 2026-11-04 that the remote authority receipt reports the
allocated SSH port, the server's `config.ssh_addr` supplies the address, the manifest port bounds
(`ssh_port_min`/`ssh_port_max`, 1024 and above) bound a reported port, and a typed refusal is
allowed until the implementation lands. On 2026-11-11 the operator clarified that the manifest
bounds are the authority's check and the server applies only the receipt bound, 1024-65535
(option B); the dispatch had read the first decision as a server-side manifest check. Approved exclusions: gdbstub and guest-agent reads over
the same TLS connection (operator); implementation PRs (follow-up issues); backfilling existing
remote Systems (operator).

No implementation plan is written: the issue is design only, and the operator confirmed on
2026-11-11 that the PR-sized follow-ups below replace the plan.

## Problem

`RemoteLibvirtConnect.recorded_ssh_endpoint` (`providers/remote_libvirt/lifecycle/connect.py`)
returns `(config.ssh_addr, port)`, reading the port from the domain XML over mutual TLS. It ignores
its `ssh_forward_port` argument. `authority_ssh_forward_port`
(`services/systems/authority_owned.py`) returns `None` for any non-local provider, so a remote
authority System always takes the domain read.

## Question 1: does the TLS connection see authority-defined domains?

No. Evidence at `2fbc81af0`:

- The remote authority's provider runs over `connection=remote_connection`, opened from
  `_remote_module_uri`: `qemu+unix:///session?socket=<provider socket>`
  (`providers/external_boot_authority/host.py`, `_open_remote_module_connection` and
  `_build_system_service`). `RemoteAuthoritySystemProvider._execute_provision` calls
  `connection.defineXML` on that connection.
- That socket belongs to a session libvirtd the authority account runs
  (`kdive-libvirtd-external-boot-authority.service`; `unix_sock_rw_perms = "0700"` in
  `deploy/systemd/libvirtd-external-boot-authority.conf`; no `--listen`). It has no TCP or TLS
  listener.
- The server and worker connect with a `qemu+tls` URI (`connection/uri_validation.py`) that the
  runbooks set to `/system` (`docs/operating/runbooks/remote-live-stack.md`), served by the host's
  privileged daemon (`libvirt_tls` role).
- libvirt's session and system drivers keep separate domain lists. ADR-0623's context states the
  same separation from the other side.

So `lookupByName` raises `VIR_ERR_NO_DOMAIN`, `_read_ssh_port` returns `None`, and
`systems.ssh_info` answers `ssh_not_provisioned`; the two job handlers raise the same reason. The
issue cannot close with a test of the current read.

## Design

Decisions 1-5 in ADR-0815, restated as the behaviour each follow-up must produce.

**Authority (follow-up 2).** `RemoteAuthoritySystemProvider._observe_provision_locked` sets
`ssh_forward_port=intent.ssh_port` on the facts it returns. `RemoteAuthoritySystemEntryV1` (or the
`RemoteAuthoritySystemManifestEntry` check it feeds) refuses `ssh_port_min < 1024`. The authority
already rejects an intent whose port leaves the entry range (`_same_subject`).

**Database.** Unchanged. 0189's `authority_system_receipt_ssh_forward_port` takes the port from any
`provision-ready` proof for that System; 0191 pins it once set; `resolve_authority_system_control_binding`
returns it only in `ready` or `activated`.

**Server (follow-up 1).** `authority_ssh_forward_port` drops its local-only early return. For a
remote-libvirt authority System it returns the stored port, or raises the ADR-0811 decision-4
refusals when none is stored. The drgn-live caller in `mcp/tools/debug/sessions/lifecycle.py`
asks only for a local-libvirt System, so remote debug attach is unchanged. `systems.ssh_info`,
`authorize_ssh_key` and `check_ssh_reachable` carry the port exactly as for local, including the
preactivation fence and the `system_not_ready` refusal in `authorize_ssh_key_handler`.

**Connector (follow-up 1).** `RemoteLibvirtConnect.recorded_ssh_endpoint(handle,
ssh_forward_port=p)`, when `p` is not `None`:

| Instance config | `p` | Result |
| --- | --- | --- |
| no `ssh_addr` or range | any | `None` (`ssh_not_provisioned`), no IO |
| `ssh_addr` set | `1024 <= p <= 65535` | `(config.ssh_addr, p)`, no domain read |
| `ssh_addr` set | outside | `configuration_error`, `reason=authority_ssh_forward_invalid`, no IO |

The instance config requires `ssh_addr` and `ssh_range` together (`remote_libvirt/config.py`), so
the first row's gate is unchanged; the instance's `ssh_range` does not bound `p`, and the authority
enforces its manifest range.

With `p is None` the existing domain read runs, so ordinary remote Systems are unchanged.

## Failure model

1. **Actors and deployments**
   - Agent clients calling the three SSH tools on the MCP server.
   - The worker running the two connectivity jobs.
   - The provider authority on a remote libvirt host, writing signed receipts.
   - Deployments: a remote-libvirt Resource with an authority binding (ADR-0623).
2. **Invariants and assets at stake**
   - The endpoint the worker sends bootstrap-key SSH to is on the configured host.
   - A stored port never changes once set (0191).
   - Ordinary remote and local Systems keep their current endpoints.
3. **Accepted failure classes**
   - Server `ssh_addr` names the wrong host: unchecked; shows as `transport_failure` from the
     reachability probe. Bounded: the destination is still operator configuration.
   - Manifest range overlapping another SSH range on the host: a domain start fails, or, while
     the authority System's domain is shut off, another guest binds its stored port and the SSH
     tools reach that guest. Bootstrap-key authentication then fails; the reachability probe can
     report it reachable. Operators keep the ranges apart; nothing enforces it (ADR-0815).
   - Remote Systems that reached `ready` before follow-up 2 keep the interim refusal; backfill is
     excluded.
4. **Covered elsewhere**
   - Receipt forgery and journal integrity: ADR-0623, ADR-0811 (trust).
   - Port rewrite after storage: migration 0191 (#3546).
   - gdbstub and guest-agent reads of authority domains: excluded, operator.

## Threat model

- **Boundaries widened:** authority receipt to database (an existing field, now set by the remote
  authority); server to worker job payload (the existing optional port field, now set for remote).
  No boundary is added.
- **Actors:** the authority host (trusted to sign receipts; already able to forge a whole provision
  result); a tenant agent calling the SSH tools (VIEWER or above, project-scoped as today).
- **Controls:** receipt model and SQL helper range 1024-65535; 0191 immutability; binding returns the
  port only in `ready`/`activated`; connector re-check of 1024-65535; address never
  read from authority data.
- **Out of scope:** an authority reporting a port outside its manifest on the configured host. It
  can already forge the whole provision result (ADR-0811); the server bounds only to 1024-65535.

## Validation

Each follow-up proves its own contracts; this design PR changes documents only
(`task-test-not-applicable`: no executable consumer reads ADR or spec prose).

| Follow-up | Contract | Mode |
| --- | --- | --- |
| 1 | remote authority System with no stored port refuses with `authority_ssh_forward_unreported` | focused-test: `ssh_info` red today with `ssh_not_provisioned` |
| 1 | the three table rows above | focused-test: connector unit tests with an injected connection asserting no open |
| 1 | drgn-live attach on a remote authority System is unchanged | focused-test: no port is resolved |
| 2 | facts and receipt carry the intent port | focused-test: provider facts and proof bytes |
| 2 | manifest refuses `ssh_port_min < 1024` | focused-test: manifest parse and Ansible validator |
| 3 | worker authorizes a key on a remote authority System | live proof on a lab host |

## Implementation follow-ups

1. **Server and worker: remote authority SSH endpoint from the stored port.** Widen
   `authority_ssh_forward_port`, keep drgn-live local-only, honour `ssh_forward_port` in
   `RemoteLibvirtConnect.recorded_ssh_endpoint`. Ships the interim refusal. Updates the local-only
   and loopback wording on every `ssh_forward_port` reader (`jobs/payloads.py`,
   `authority_owned.py`, `ssh_access.py`, `providers/ports/lifecycle.py`, the debug attach
   docstring, the connectivity handlers' error text), and appends the ADR-0811 amendment ADR-0815
   names. Deploy workers first.
2. **Authority: report the remote System's SSH port.** Facts carry `intent.ssh_port`; manifest
   minimum 1024 in the authority and in
   `deploy/ansible/scripts/validate_authority_system_provisioning.py`; the "loopback" wording in
   `providers/system_authority/protocol.py` covers remote. Requires servers and workers at or after
   ADR-0811; check manifests before upgrading, since a sub-1024 range stops the authority.
3. **Operations: remote authority SSH documentation and live proof.** Document that the instance
   declares `ssh_addr` (with an `ssh_range`, which config requires), that `ssh_addr` must reach the authority's hostfwd and that manifest SSH ranges should not overlap
   other SSH ranges on the host; run `systems.authorize_ssh_key` against a remote authority System.
