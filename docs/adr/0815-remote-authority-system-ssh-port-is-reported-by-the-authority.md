# 0815 — A remote authority System's SSH port is reported by the authority

## Status

Accepted (2026-11-11)

## Context

A remote-libvirt System gets its SSH endpoint from `RemoteLibvirtConnect.recorded_ssh_endpoint`:
the address is the instance's `ssh_addr` and the port is read from the domain's hostfwd in its
live XML, over the server's or worker's mutual-TLS connection
([ADR-0291](0291-remote-ssh-bootstrap-injection.md)). ADR-0811 replaced that read with a port the
authority reports, but only for local-libvirt authority Systems, and left remote unchanged (#3547).

The read cannot work for a System the remote authority provisioned. The TLS URI names the remote
host's system daemon (`qemu+tls://<host>/system`, `docs/operating/runbooks/remote-live-stack.md`).
The authority defines its domains on a different daemon: a session libvirtd run by the authority
account, opened as `qemu+unix:///session?socket=…` (`_remote_module_uri`,
`providers/external_boot_authority/host.py`). That daemon listens only on a mode-0700 unix socket
under `/run/kdive/provider-authority/libvirt` (`deploy/systemd/libvirtd-external-boot-authority.conf`),
and its unit starts it without `--listen` (`provider_authority_host/tasks/libvirt.yml`). Session and system daemons keep separate domain lists,
which is why ADR-0623 routes provisioning to the authority at all. The server's `lookupByName`
therefore gets `VIR_ERR_NO_DOMAIN`, `_read_ssh_port` returns `None`, and the SSH tools report
`ssh_not_provisioned`, a reason that sends the operator to fix configuration that is correct.

The authority already holds the answer. It allocates the System's port from its manifest entry's
`ssh_port_min`..`ssh_port_max`, records it in its private provision intent, and renders it into
the hostfwd the domain keeps across activation (the ADR-0583 preserved definition excludes only
the boot fields).

## Decision

The operator decided this on 2026-11-04.

1. **The remote authority reports the port.** Its `provision-ready` facts carry
   `ssh_forward_port` from the intent's `ssh_port`, the same optional field the local authority
   fills (ADR-0811 decision 1). The remote manifest refuses an entry whose `ssh_port_min` is below
   1024, so a range the receipt model would reject fails at authority startup, not at provision.
2. **Storage is ADR-0811's, unchanged.** The finalizer and repair copy the port into
   `authority_system_ownership.ssh_forward_port` through `authority_system_receipt_ssh_forward_port`,
   which does not look at the provider kind. Migration 0191 (#3546) makes the value immutable once
   set. No migration, no new column, no receipt or payload shape change.
3. **The server supplies the address.** The endpoint is the instance's configured `ssh_addr` and
   the stored port. The receipt never carries an address. Before any network IO, the connector
   requires the port to lie in the instance's own `ssh_port_min`..`ssh_port_max`, which the operator
   sets to cover the authority manifest's ranges for that Resource; a port outside it fails with
   `configuration_error`, `reason=authority_ssh_forward_invalid`. An instance with no SSH forward
   configured keeps returning `None` (`ssh_not_provisioned`).
4. **The server path is ADR-0811's, widened to remote.** `authority_ssh_forward_port` returns the
   stored port for a remote-libvirt authority System too, and the three SSH tools carry it, in the
   same optional `AuthorizeSshKeyPayload` and `CheckSshReachablePayload` fields the #3541 amendment
   added, to `RemoteLibvirtConnect.recorded_ssh_endpoint`, which uses it in place of the domain
   read. The drgn-live attach path keeps asking only for a local-libvirt System, because a remote
   drgn-live attach rides the guest agent, not SSH.
5. **Interim refusal.** Until decision 1 ships, a remote authority System stores no port, and the
   widened `authority_ssh_forward_port` refuses it with `authority_ssh_forward_unreported` (or
   `authority_system_not_attachable` outside `ready` and `activated`) instead of
   `ssh_not_provisioned`.

## Consequences

- **Upgrade order:** any order fails safe. An older server ignores a reported remote port and
  keeps the failing domain read. An older worker ignores the carried port on the remote connector
  and reads the domain, which returns `ssh_not_provisioned`. Workers first, then servers, then
  authorities gives the full path soonest. The lifecycle identity does not move.
- **Existing Systems:** a remote authority System that reached `ready` before decision 1 has no
  stored port and keeps the interim refusal. Backfill is the operator's.
- **Deployment contract:** the server instance's `ssh_addr` must reach the hostfwd the manifest
  entry binds, and its range must cover the manifest ranges. Nothing checks the address; a wrong one
  shows up as `systems.check_ssh_reachable` failing with `transport_failure`.
- **Port contention:** the authority picks free ports from the domains its session daemon sees. It
  cannot see the system daemon's domains, so ranges shared with ordinary Systems on the same host can
  collide. A collision fails the second domain start; it cannot misroute a connection.
- **Not covered:** gdbstub and guest-agent reads over the same TLS connection have the same
  invisibility. They are the operator's to decide and are unchanged here.
- **Implementation follow-ups**, each one PR:
  1. *Server and worker:* widen `authority_ssh_forward_port` to remote-libvirt (the interim
     refusal), keep the drgn-live caller local-only, and make
     `RemoteLibvirtConnect.recorded_ssh_endpoint` honour `ssh_forward_port` with the range check.
  2. *Authority:* report `ssh_forward_port` from the remote intent and refuse a manifest
     `ssh_port_min` below 1024; update the receipt field's "loopback" wording.
  3. *Operations:* document the deployment contract above and prove `systems.authorize_ssh_key`
     against a remote authority System on a lab host.

## Considered & rejected

- **Keep the domain read (do nothing).** verified: at `2fbc81af0`,
  `rg -n "def _remote_module_uri" -A1 src/kdive/providers/external_boot_authority/host.py` returns
  `qemu+unix:///session?socket=…`, and the TLS URI must use scheme `qemu+tls`
  (`connection/uri_validation.py`), which the deployed runbooks point at `/system`. The two
  daemons do not share a domain list, so the read finds no domain.
- **Let the server reach the authority's daemon** (a TLS listener on the session daemon, or the
  authority defining domains on the system daemon). judgment: ADR-0623 makes the authority the sole
  mutator of a private daemon, and ADR-0811 keeps the server unable to address it.
- **The receipt also reports the address.** judgment: the worker sends SSH traffic authenticated with
  the System's bootstrap key to that endpoint; a signed address would let authority-written data
  choose the destination host. Server configuration already names it.
- **A remote-specific column or table.** verified: `rg -n provider_kind
  src/kdive/db/schema/0189_authority_system_ssh_forward.sql` at `2fbc81af0` matches lines 88 and 92
  only, the binding function's column list; the receipt helper and the column check never read it,
  so 0189 already stores either kind.
- **Bound the port only by the receipt's 1024-65535.** judgment: the instance range bounds the
  destination to ports the operator declared on that host, at the cost of keeping two ranges aligned.
