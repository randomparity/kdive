# ADR 0077 — qemu+tls:// control transport + x509 client-cert secret-by-reference (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  remote-libvirt package this transport serves), [ADR-0012](0012-secret-backend.md) /
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the register-before-return
  `SecretBackend` the cert resolves through), [ADR-0073](0073-forced-secret-resolution-redaction.md)
  / [ADR-0075](0075-objectstore-quarantine-pre-registration-writes.md) (the
  register-before-emit / release-after-persist redaction contract this is the first real
  provider to exercise).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md)

## Context

`local_libvirt` connects to `qemu:///system` on the same host (the connection URI is already
read from `KDIVE_LIBVIRT_URI`, `discovery.py`). M2's host is **remote** and the MCP/worker tier
does not share its filesystem or trust domain. libvirt offers two production remote transports:
`qemu+ssh://` (tunnels control and, via the same key, file transfer) and `qemu+tls://` (x509
mutual-TLS to a TLS-listening libvirtd, control only).

Bulk artifact movement is decided separately (ADR-0078: the object store, not the control
channel), which removes the main reason to prefer SSH (its file-transfer convenience). What
remains is the control channel and its secret. M2 is also the **first** provider to resolve a
real secret — local-libvirt resolves none, and M1.5's fault-inject resolved only a synthetic
sentinel (ADR-0073) — so the choice must exercise the secret-by-reference→redaction contract
against production-shaped credential material.

## Decision

We will use **`qemu+tls://` for the remote-libvirt control plane** (discovery capability
enumeration, provisioning define/start, control power/reset/force-crash). The **x509 client
cert and key are a secret-by-reference**: the resource's `capabilities` jsonb carries a
`secret_ref`, never the material itself; the worker resolves it through the runtime's
`SecretBackend`, which **registers the resolved value into the redaction registry before
returning it** (ADR-0027), and the per-op scope is **released only after** redact-and-persist
(ADR-0075). Bulk files do **not** ride this channel (ADR-0078); the live gdbstub does **not**
ride it either (ADR-0079).

## Consequences

- **Control and bulk data are decoupled.** A large vmcore transfer cannot stall or be coupled
  to the libvirt control channel, and the control channel carries one credential kind (the TLS
  cert), not also an SSH key reused for sftp.
- **M2 proves the real-secret half of the contract.** The TLS cert is the first production
  secret the platform resolves; the register-before-emit / release-after-persist path
  (ADR-0073/0075), previously exercised only by a synthetic sentinel, now runs against real
  credential material, so any guest-agent transcript or console capture that echoes the cert is
  exact-value masked.
- **Operational cost: TLS material management.** Each remote host needs a libvirtd TLS listener
  and the platform needs a client cert per host (or CA-issued), provisioned into the secret
  backend and referenced by `secret_ref`. This is heavier than an SSH key but is the standard
  multi-tenant libvirt posture and avoids planting an SSH login on every host.
- **Reachability assumption (documented).** The worker must reach the host's libvirtd TLS port;
  this is an operator network responsibility, recorded with the gdbstub-port assumption
  (ADR-0079).
- **A connect/auth failure maps to `transport_failure`** — no new `ErrorCategory`.

## Alternatives considered

- **`qemu+ssh://` (SSH-unified).** Collapses control + file transfer + a gdbstub local-forward
  onto one SSH identity and one channel — operationally simple. Rejected as the M2 base because
  bulk movement is the object store (ADR-0078), which removes SSH's main advantage, and because
  an SSH login on every remote host is a broader standing-access surface than a scoped TLS
  client cert in a multi-tenant service; TLS also matches the "MCP server separate from dev
  hosts" deployment without granting shell access.
- **`qemu+tls://` control + `virStorageVolUpload` for files (no object store).** Single channel,
  single secret. Rejected with ADR-0078: direct-kernel boot from a storage-pool path does not
  generalize past M2, and the object store is the only artifact channel present in every later
  milestone.
- **Unauthenticated `qemu+tcp://`.** Rejected outright — no mutual auth, unacceptable for a
  multi-tenant service reaching hosts over a network.
