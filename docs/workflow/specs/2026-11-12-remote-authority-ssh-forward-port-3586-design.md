# Remote authority reports ssh_forward_port — design (#3586)

Governing decision: [ADR-0815](../../adr/0815-remote-authority-system-ssh-port-is-reported-by-the-authority.md)
decision 1, Consequences "Manifest floor", follow-up 2. This spec records no new decision.

## Problem

The remote-libvirt authority allocates a System's guest SSH port from its manifest range and
stores it in its private intent (`_ProvisionIntentV1.ssh_port`), but its provision facts omit
`ssh_forward_port`, so the receipt carries no port and the server has nothing to store. The
local authority already fills the field. The receipt model bounds the port to 1024-65535
(`SshForwardPort`), while the remote manifest entry accepts any `ssh_port_min >= 1`; a manifest
reaching below 1024 would only fail when a provision reports the port.

## Scope

1. `RemoteAuthoritySystemProvider._observe_provision_locked`
   (`src/kdive/providers/remote_libvirt/system_authority.py`) returns
   `ssh_forward_port=intent.ssh_port` when the facts are complete, and `None` otherwise — the
   local authority's rule (port only on complete facts). The absent-intent facts in
   `_observe_provision` stay portless. Storage and the receipt shape are ADR-0811's, unchanged;
   `service.py` already copies facts into the `provision-ready` receipt for either provider.
2. `RemoteAuthoritySystemManifestEntry.__post_init__` refuses `ssh_port_min < SSH_FORWARD_PORT_MIN`
   (1024, imported from `providers/system_authority/protocol.py`) with
   `ValueError("authority ssh port range starts below 1024")`. The remote manifest model
   (`manifest.py`) builds these entries in its `_topology_is_valid` validator, so loading the
   manifest at authority startup refuses it. No change to `manifest.py` is needed.
3. The Ansible preflight `deploy/ansible/scripts/validate_authority_system_provisioning.py`
   refuses a remote entry with `ssh_port_min < 1024` through its existing `_fail()`. The script
   is stdlib-only and cannot import kdive, so it carries the literal `1024` beside the
   gdb/ssh range check.
4. The comments on `SSH_FORWARD_PORT_MIN` and the receipt field in `protocol.py` drop the
   "loopback"/local-only wording: the port is the guest SSH forward port an authority reports,
   loopback for a local authority and on the configured `ssh_addr` for a remote one.

No ownership change: each check extends its existing owner. Exclusions per WORK:SCOPE: server
and worker consumption (#3585), operator docs and live proof (#3594), backfill, cross-daemon
collision checks, any `gdb_port_min` floor.

## Failure model

1. **Actors and deployments** — the authority-host operator who writes the private manifest
   and runs the Ansible deploy; the remote-libvirt authority daemon at startup and on provision.
2. **Invariants and assets at stake** — authority availability on that host: a manifest the
   floor refuses stops the authority from starting, halting every authority-lane operation
   there (ADR-0815 accepted this). The runtime check and the preflight must agree on 1024 so a
   deploy that passes preflight starts.
3. **Accepted failure classes** — an intent persisted before this change with a port below
   1024 cannot reach the new facts: the floor refuses that manifest at startup, and after the
   operator raises the range, `_same_subject` refuses the old intent as a CONFLICT. A remote
   System completed before this change keeps no stored port (backfill excluded, ADR-0815).
4. **Covered elsewhere** — the port's range on the server side and its 1024-65535 recheck
   (#3585); storage immutability (migration 0191); port contention across daemons
   (ADR-0815 Consequences, operator's).

## Success

- A completed remote provision's facts (execute and observe) carry `ssh_forward_port` equal to
  the intent's `ssh_port`; absent-intent facts have `ssh_forward_port is None`.
- A remote manifest entry with `ssh_port_min` 1023 is refused when the manifest loads and by the
  preflight; 1024 is accepted by both.
- `protocol.py` no longer calls the port loopback-only.
- No schema, receipt, payload, or Operation change; the lifecycle identity hash does not move.

## Validation

- facts port — `focused-test`: `tests/providers/remote_libvirt/test_system_authority.py`, extend
  the complete-provision, absent-observation, and interrupted (incomplete-intent) tests; red:
  `facts.ssh_forward_port is None` on the complete path.
- runtime floor — `focused-test`: `tests/providers/system_authority/test_manifest.py`
  parametrized 1023 refused / 1024 accepted, with `ssh_port_max = ssh_port_min + 2` so the range
  stays clear of the fixture's gdb range; red: 1023 loads.
- preflight floor — `focused-test`: `tests/deploy/test_provider_authority_local_provisioning.py`
  parametrized 1023 / 1024 through `_remote_validator_input`, same `ssh_port_max`; red: 1023
  exits 0.
- comment wording — `task-test-not-applicable`: prose comment, no executable consumer.
- lifecycle identity — `focused-test`: `lifecycle_protocol_identity()` printed on `origin/main`
  and on the branch must be identical strings.
