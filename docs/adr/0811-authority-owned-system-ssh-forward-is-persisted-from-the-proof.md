# 0811 — An authority-owned System's SSH forward is persisted from its provision receipt

## Status

Accepted (2026-11-04)

## Context

`debug.start_session(transport=drgn-live)` resolves the System's loopback SSH forward by reading
its domain XML through the process's own libvirt endpoint
(`providers/local_libvirt/lifecycle/connect.py`). A domain that an authority provisioned lives in
the authority's private libvirt daemon ([ADR-0623](0623-authority-owned-system-provisioning.md)),
so the lookup fails with `system_domain_not_found` and no debug attach or `introspect.script` can
reach an authority-owned System (#3536).

The attach runs in the MCP server. The server holds no authority credential: every authority call
is signed with the worker incarnation credential
([ADR-0611](0611-worker-local-authority-unix-route.md),
[ADR-0770](0770-installed-worker-authority-route-is-published.md)), and the server's runtime
carries the authority's metadata only. The server must also stay unable to address the authority's
libvirt daemon. The authority allocates the forward port itself and keeps it in a private intent,
and neither authority protocol reported it.

## Decision

1. **The authority reports the port in its `provision-ready` receipt.** The proof and the provider
   facts gain an optional `ssh_forward_port`, an integer in 1024-65535 (strict: a string, float or
   boolean is refused). It is excluded from the canonical bytes when absent, so a receipt written
   before this change keeps its bytes and still parses. The local authority fills it from its
   persisted intent. The remote authority does not.
2. **The database copies it, bound to the System.** Migration 0189 adds
   `authority_system_ownership.ssh_forward_port` with the same range check. The terminal finalizer
   and the reconciler repair, the two places that move an ownership row to `ready`, set it from the
   attempt's stored receipt bytes through `authority_system_receipt_ssh_forward_port(receipt,
   system_id)`. The helper returns NULL unless the receipt is a `provision-ready` proof naming that
   exact System with an in-range integer. A forged, foreign or malformed value stores nothing.
3. **The server reads it through the existing binding.**
   `resolve_authority_system_control_binding` returns the port only while the ownership state is
   `ready` or `activated`. The attach path reads it for a local-libvirt authority-owned System and
   hands it to `Connector.open_transport(..., ssh_forward_port=)`. The local connector uses
   `127.0.0.1` and the port in place of the domain-XML read, re-checks the range, and still runs the
   SSH banner probe. No other connector uses the argument, and it is passed only for an
   authority-owned System, so the fixed-worker path is unchanged.
4. **Absence fails closed.** An authority-owned local System with no reported port fails with
   `configuration_error` and `reason=authority_ssh_forward_unreported`, or
   `authority_system_not_attachable` outside `ready` and `activated`. The message tells the operator
   to reprovision against an upgraded authority. There is no fallback to the server's own libvirt
   endpoint. A reported port outside the range fails with `authority_ssh_forward_invalid` before any
   network IO.

## Consequences

- **Schema:** one nullable column, one helper function, and replacements of the finalizer, the
  repair and the binding function. Roll-forward only ([ADR-0015](0015-forward-only-migrations.md)).
- **Wire:** the receipt shape grows. A server and worker built from this change accept an older
  authority's receipts, which carry no port, and fail closed at attach. An older worker rejects a
  newer authority's receipt, because the proof model forbids unknown fields. Upgrade workers
  before authorities. The lifecycle identity hash moves with the proof model, so the native live
  runner must be refreshed.
- **Existing Systems:** an authority System that reached `ready` before this change has no stored
  port. Debug attach refuses it with the reprovision message. Reprovision is unsupported for an
  authority-owned System ([ADR-0623](0623-authority-owned-system-provisioning.md)), so it has to be
  torn down and provisioned again.
- **Trust:** the port is data the authority signed into its journal chain and the database re-checks.
  It names a host port the authority itself opened on loopback. Anything able to forge a receipt can
  already forge the whole provision result.
- **Not covered:** `systems.ssh_access` and the connectivity job handlers read the same recorded
  endpoint through `recorded_ssh_endpoint` and do not use the stored port. Remote-libvirt authority
  Systems keep their existing behaviour.

## Considered & rejected

- **Give the server an authority route and ask the authority.** verified: the sender signs with the
  worker incarnation credential and the server has none. judgment: handing the web-facing process a
  credential that can ask the authority about Systems widens a custody boundary for a value that
  never changes after provision.
- **A new authority operation resolved by the worker.** judgment: the worker is not on the attach
  path, and the value would still need a place to live between calls.
- **Derive the port from the System id.** judgment: the authority picks a free ephemeral port and
  must be free to move it, and the server cannot know the authority's range.
- **Keep the port outside the ownership row.** judgment: the row already carries the System's
  immutable authority identity and is the one the binding function reads, so a second table adds a
  join and a grant for one integer.
