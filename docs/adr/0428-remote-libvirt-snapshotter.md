# ADR 0428 — Remote-libvirt Snapshotter (the deferred ADR-0378 opt-in)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers

## Context

ADR-0378 added System snapshot/restore as a provider-advertised, System-scoped checkpoint and
wired it for local-libvirt only, recording the deferral verbatim: "Local-libvirt only in this
change; the port makes remote-libvirt a later opt-in." This ADR is that opt-in (#1430, part of the
remote-libvirt parity epic #1423).

The forces are already resolved by the ADR-0378 design and need no re-litigation:

- The snapshot surface keys on capability, not provider identity. `systems.snapshot` /
  `restore` / `list_snapshots` / `delete_snapshot` gate on `ProviderSupport.supports_snapshots` and
  return `capability_unsupported` on a miss; `jobs/handlers/systems.py` carries a defence-in-depth
  `runtime.snapshot is None` backstop. So wiring the port and setting the flag makes all four tools
  reachable with no change outside the provider package.
- The `Snapshotter` port (`providers/ports/lifecycle.py`) is narrow and domain-name-keyed:
  `create(domain_name, name, *, include_memory)`, `revert(domain_name, name, *, start_paused)`,
  `delete`, `delete_all`. Nothing in the signature assumes a local connection.
- Snapshots are internal (stored inside the domain's disk image), so `delete`/`delete_all` free the
  data with no external object-store cleanup — the same property holds over `qemu+tls://`.
- The teardown reclaim path (`_reclaim_snapshots` in `jobs/handlers/systems.py`) is
  provider-agnostic: it calls `runtime.snapshot.delete_all` when the port is present and skips when
  it is `None`. Wiring the remote port makes that path reclaim remote snapshots with no change.

## Decision

We will add `RemoteLibvirtSnapshotter`, a `Snapshotter` realized against the remote host over the
existing `qemu+tls://` transport, and advertise `supports_snapshots=True` together with the wired
port in `remote_libvirt/composition.py`.

The implementation mirrors `LocalLibvirtSnapshotter`'s libvirt `virDomainSnapshot*` mechanics
(memory-vs-disk-only create with same-name pre-delete, running-vs-paused revert, idempotent
delete/delete_all treating an absent domain or snapshot as success). Only the connection lifecycle
differs: instead of a bare `libvirt.open`, each op runs inside the shared
`remote_connection` context manager (mutual-TLS materialize → connect → cleanup, ADR-0077), exactly
as `RemoteLibvirtControl` does. There is no shared code layer with local-libvirt (ADR-0076); the
snapshot mechanics are duplicated deliberately.

Error categories match the `Snapshotter` port contract for operational faults —
`INFRASTRUCTURE_FAILURE` for a libvirt snapshot/revert/delete fault or an absent domain,
`CONFIGURATION_ERROR` for a missing snapshot on revert. Connection-establishment errors inherit the
shared transport's taxonomy (as every remote-libvirt port does): `CONFIGURATION_ERROR` for an
unsafe URI or unresolvable TLS secret refs — the port contract's "invalid provider connection
configuration" — and `TRANSPORT_FAILURE` when the mutual-TLS connect itself fails. No migration:
the port and flag are provider-composition state, not schema.

## Consequences

- A remote System gains the ADR-0378 "snapshot just before the bug, restore, retry" loop, dropping
  a panic→retry repro cycle from minutes to seconds. `systems.get.data.supports_snapshots` reports
  `true` for a remote System, and all four snapshot tools become reachable.
- `#1428` (the capability-parity guard) will enforce that `supports_snapshots` and the `snapshot`
  port agree; this change sets both, so it satisfies that pairing rather than tripping it.
- Open design points, stated but unenforced (per the issue):
  - **Remote storage capacity accounting.** The remote boots an operator-staged qcow2 on the remote
    host's pool (ADR-0080/0112). Internal snapshots grow that volume on the *remote* host — capacity
    impact lands where kdive does not account for it. Stated explicitly; nothing enforces a quota.
  - **`include_memory` transport-agnosticism.** A RAM+disk checkpoint of a remote domain is written
    on the remote host by libvirt; the call is transport-agnostic in signature. The live proof
    confirms it in practice.
  - **Teardown reclaim.** Snapshots are System-scoped and released with the System via the existing
    provider-agnostic `_reclaim_snapshots` best-effort `delete_all`; the live proof confirms nothing
    remains on the remote pool.

## Alternatives considered

- **A shared snapshot layer between local and remote.** Rejected: ADR-0076 forbids a shared
  provider layer; the connection lifecycles genuinely differ (bare open vs mutual-TLS pkipath), and
  the snapshot XML/flag mechanics are a handful of lines. Duplication is the established pattern
  (`RemoteLibvirtControl` vs `LocalLibvirtControl`).
- **Mapping the transport's `TRANSPORT_FAILURE` to `INFRASTRUCTURE_FAILURE` to match the port
  docstring literally.** Rejected: every other remote-libvirt port surfaces the transport's
  `TRANSPORT_FAILURE` for a TLS connect fault, and the platform's typed taxonomy distinguishes a
  transport fault from a provider-side infrastructure fault for a reason. The `Snapshotter`
  docstring enumerates the local categories; a remote realization legitimately extends them, as the
  `Controller` port already does.
