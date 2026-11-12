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

The operator decided this on 2026-11-04 and clarified decision 3 on 2026-11-11. The campaign
dispatch had relayed the 2026-11-04 decision as a server-side check against the manifest bounds,
which the server cannot perform: the manifest is private authority-host configuration (ADR-0623)
with no server-side reader. The operator's clarification is that the manifest bounds are the
authority's check, and the server applies only the receipt bound.

1. **The remote authority reports the port.** Its `provision-ready` facts carry
   `ssh_forward_port` from the intent's `ssh_port`, the same optional field the local authority
   fills (ADR-0811 decision 1). The remote manifest refuses an entry whose `ssh_port_min` is below
   1024, so a range the receipt model would reject fails at authority startup, not at provision.
2. **Storage is ADR-0811's, unchanged.** The finalizer and repair copy the port into
   `authority_system_ownership.ssh_forward_port` through `authority_system_receipt_ssh_forward_port`,
   which does not look at the provider kind. Migration 0191 (#3546) makes the value immutable once
   set. No migration, no new column, no receipt or payload shape change.
3. **The server supplies the address; the authority alone bounds the port.** The endpoint is the
   instance's configured `ssh_addr` and the stored port. The receipt never carries an address.
   The server checks the port only against the receipt bound, 1024-65535, re-checked in the
   connector before any network IO as the local connector does; a port outside it fails with
   `configuration_error`, `reason=authority_ssh_forward_invalid`. The manifest range is enforced
   by the authority alone, when it allocates and when it adopts an intent (`_same_subject`). An
   instance with no SSH forward configured keeps returning `None` (`ssh_not_provisioned`). The
   instance config requires `ssh_addr` and `ssh_range` together (`remote_libvirt/config.py`), so the
   instance still declares a range, which is not a bound on the reported port.
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

- **Upgrade order:** given servers and workers at or after ADR-0811, any order fails safe. An older
  server ignores a reported remote port and keeps the failing domain read. An older worker ignores
  the carried port on the remote connector and reads the domain, which returns
  `ssh_not_provisioned`. A server or worker older than ADR-0811 forbids the receipt field, so it
  must be upgraded before follow-up 2's authority, as ADR-0811 already requires for local
  authorities. Workers, then servers, then authorities gives the full path soonest. The lifecycle
  identity does not move.
- **Manifest floor:** after follow-up 2 a remote manifest with `ssh_port_min` below 1024 stops the
  authority from starting, which stops every authority-lane operation on that host, not only SSH.
  Check manifests before upgrading the authority.
- **Existing Systems:** a remote authority System that reached `ready` before decision 1 has no
  stored port and keeps the interim refusal. Backfill is the operator's.
- **Address:** the server instance's `ssh_addr` must reach the hostfwd the manifest entry binds,
  as it must for any remote System today. Nothing checks it; a wrong one shows up as
  `systems.check_ssh_reachable` failing with `transport_failure`.
- **Port contention:** the authority picks free ports from the domains its session daemon sees
  (`used_ssh_ports`), not the system daemon's. A manifest range that overlaps another SSH range on
  the same host can collide: a domain start fails, or, while the authority System's domain is shut
  off, another guest binds its stored port and the SSH tools reach that guest. The reachability
  probe can then report it reachable, and bootstrap-key authentication fails. Operators avoid this
  by keeping the ranges apart; nothing enforces it.
- **Trust:** the server accepts any port in 1024-65535 on the configured host. An authority that
  reports a port outside its manifest is already able to forge the whole provision result
  (ADR-0811).
- **ADR-0811 claims this supersedes in part,** once the behaviour lands: decision 1's "The remote
  authority does not", decision 3's "No other connector uses the argument", and the #3541
  amendment's "written only for an authority-owned local System" and "Remote-libvirt authority
  Systems keep their existing behaviour". Follow-up 1 appends the ADR-0811 amendment.
- **Not covered:** gdbstub and guest-agent reads over the same TLS connection have the same
  invisibility. They are the operator's to decide and are unchanged here.
- **Implementation follow-ups**, each one PR:
  1. *Server and worker:* widen `authority_ssh_forward_port` to remote-libvirt (the interim
     refusal), keep the drgn-live caller local-only, and make
     `RemoteLibvirtConnect.recorded_ssh_endpoint` honour `ssh_forward_port` with the 1024-65535
     check; update the local-only and loopback wording on every `ssh_forward_port` reader
     (`jobs/payloads.py`, `authority_owned.py`, `ssh_access.py`, `providers/ports/lifecycle.py`, the
     debug attach docstring, and the connectivity handlers' error text), and append the ADR-0811
     amendment above.
  2. *Authority:* report `ssh_forward_port` from the remote intent and refuse a manifest
     `ssh_port_min` below 1024, in the authority and in the Ansible preflight
     (`deploy/ansible/scripts/validate_authority_system_provisioning.py`); update the "loopback"
     wording in `providers/system_authority/protocol.py`.
  3. *Operations:* document the address and port-contention consequences above for remote
     authority hosts, including that the instance declares `ssh_addr` with an `ssh_range`, and prove `systems.authorize_ssh_key` against a remote authority System on a
     lab host.

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
- **The server checks the port against the manifest bounds.** verified: ADR-0623's Decision keeps
  the manifest as operator-owned private host configuration that requests never carry, and
  `rg -ln RemoteAuthoritySystemManifestV1 src/kdive` at `2fbc81af0` lists only
  `providers/system_authority/` and `providers/external_boot_authority/host.py`, so no server code
  can read it.
- **The server checks the port against its instance `ssh_range`, set to cover the manifest
  ranges.** judgment: a second copy of the manifest bounds the operator must keep aligned, for a
  value the authority already bounds; the operator chose the receipt bound alone (2026-11-11).
