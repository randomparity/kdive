# Remote authority System SSH port: server and worker (#3585)

[ADR-0815](../../adr/0815-remote-authority-system-ssh-port-is-reported-by-the-authority.md) follow-up 1.

## Problem

`authority_ssh_forward_port` returns `None` for a remote-libvirt authority System, and
`RemoteLibvirtConnect.recorded_ssh_endpoint` ignores `ssh_forward_port`, so the SSH tools read a
domain the server's TLS daemon cannot see and report `ssh_not_provisioned`.

## Scope

- `authority_ssh_forward_port(conn, system_id, *, local_only=False)` drops the local-only return:
  any authority System gets its stored port or the ADR-0811 refusals
  (`authority_ssh_forward_unreported`, `authority_system_not_attachable`). `local_only=True`
  keeps the old `None` for a remote System; only the drgn-live attach caller passes it.
- `RemoteLibvirtConnect.recorded_ssh_endpoint`, given a port: refuses one outside 1024-65535 with
  `configuration_error`, `reason=authority_ssh_forward_invalid`; then returns `None` when the
  instance has no `ssh_addr`/`ssh_range`; else `(ssh_addr, port)` with no libvirt connection.
  Bounds reuse `SSH_FORWARD_PORT_MIN`/`MAX` from `providers/system_authority/protocol.py`.
- `ssh_access.py`, the handlers and payloads keep their logic; the job tools' pre-activation fence
  still runs before the port read, and `ssh_info` now narrows a remote System's next actions too.
- Wording: drop "local"/"loopback" on the `ssh_forward_port` readers named in ADR-0815 follow-up
  1, the `ssh_not_provisioned` texts and the three tool descriptions in `registrar.py`
  (regenerating `docs/guide/reference/systems.md`). Append an ADR-0811 amendment. No ownership
  transition: a clean extension of the ADR-0811 path.

### Failure model

1. Actors and deployments: an authenticated MCP caller on a server with a remote-libvirt
   instance; the worker running the two SSH jobs.
2. Invariants: the endpoint host is always the operator's `ssh_addr`, never receipt data; a
   remote System without SSH parity keeps `ssh_not_provisioned`; remote drgn-live attach is
   unchanged; no payload, schema or lifecycle-identity change.
3. Accepted: a remote authority System provisioned before #3586 stores no port and is refused
   (ADR-0815 decision 5). A wrong `ssh_addr` surfaces as `transport_failure`; port contention
   can reach another guest, reported reachable, and key authorization fails (ADR-0815).
4. Covered elsewhere: producing the port and the manifest floor (#3586); operator docs and live
   proof (#3594).

## Success

1. With a stored port, `ssh_info` returns `(ssh_addr, port)` in ownership `ready`/`activated`;
   in `activated` both job tools enqueue a payload carrying it.
2. With none stored, `ssh_info` (and the job tools in `activated`) refuse with
   `authority_ssh_forward_unreported`; `ssh_info` outside `ready`/`activated` refuses with
   `authority_system_not_attachable`; the job tools keep their fence ahead of the port read.
3. drgn-live attach on a remote authority System never calls the refusal path.
4. The remote connector refuses 1023 and 65536, accepts 1024 and 65535, and opens no libvirt
   connection for a carried port; with SSH parity unset it returns `None`.

## Validation

- Success 1-2: focused-test, `test_systems_ssh_access.py` remote cases replacing the two tests
  that pin the old remote behaviour; red before (`None` forward, port-free payload).
- Success 3: focused-test, `tests/mcp/debug/test_debug_tools.py` remote authority drgn-live
  case asserting no refusal.
- Success 4: focused-test, `tests/providers/remote_libvirt/lifecycle/test_connect.py` bound and
  no-IO cases with an `open_connection` that fails if called.
- Wording, ADR amendment: task-test-not-applicable; prose only (`just docs` checks the reference).
