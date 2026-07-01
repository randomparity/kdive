# ADR 0289 — Per-System SSH bootstrap key, injected at provision

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers
- **Supersedes:** ADR-0052 (standing managed key)
- **Revises:** ADR-0271 (`authorize_ssh_key` key source), the drgn-live transport (#762/#697), and
  the ADR-0288 "the managed authorized key stays on `--ssh-inject`" bullet.

## Context

Catalog rootfs images bake a standing root SSH key into the base QCOW2 (ADR-0052): the family
customizers `--ssh-inject root:file:<managed pubkey>`, and the worker holds the matching private
half to root-SSH a provisioned System for `authorize_ssh_key` (ADR-0271) and live drgn
introspection (#762/#697). The #962 live proof exposed two defects:

1. The baked key is an **immortal, deployment-wide credential** — its public half is in every
   catalog image, its private half roots every System ever provisioned from any image, and
   rotating it means re-baking the whole catalog. The stored image is itself a root-granting
   artifact.
2. `prereqs/managed_ssh_key.py` lazily generates the keypair **per host** under XDG, so a build
   principal (`dave`) and a worker principal (`root`) diverge and `authorize_ssh_key` fails
   `Permission denied` (exit 255).

The root cause is a category error: the keypair is host-local ambient state when it is per-System
infrastructure state. See
`docs/superpowers/specs/2026-07-01-per-system-ssh-bootstrap-key-design.md`.

## Decision

Generate a **unique bootstrap keypair per System at provision time** and inject its public half
into the per-System overlay. The base image bakes no authorized key.

- **The `systems` job handlers own the secret.** They hold the DB connection, so they generate the
  keypair once, store the private half in a new `system_bootstrap_keys` table
  (`system_id` PK, `ON DELETE CASCADE`), load it for `authorize`/drgn, and delete it at teardown.
  The key is registered with `SecretRegistry` on load.
- **The provider owns the overlay mutation.** `provision()` runs an ordered list of overlay
  customizers against the per-System overlay **only when it creates the overlay** (retry-safe),
  before start. Key-injection (`virt-customize --ssh-inject`, off the cloud-init path) is the first
  customizer; the seam is **extensible** so future provision-time mutations append a customizer
  rather than adding parallel one-offs.
- **The two SSH consumers re-source their key.** `authorize_ssh_key` and drgn-live load the
  per-System private key instead of `managed_private_key_path()`; `managed_ssh_key.py` and the
  build-time `--ssh-inject` are deleted.

This is dictated by the connectionless provider seam: the handler (transactional, has `conn`) owns
the secret; the provider (connectionless, owns libguestfs) owns the injection.

## Consequences

- No shared or immortal credential anywhere: compromise blast radius is one System for its
  lifetime; no host-to-host key distribution; no re-bake to rotate; stored images carry no
  credential and are safe to treat as public.
- The provision hot path gains one libguestfs pass (`--ssh-inject` on the overlay, seconds) and a
  small `ssh-keygen`; both are bounded and per-System.
- kdive now custodies an ephemeral per-System bootstrap **private** key in Postgres. This does not
  violate ADR-0271's "KDIVE never holds the *agent's* key" — it is kdive's own infra key for its
  own worker operations, scoped to one System and deleted at teardown. At-rest protection is DB
  access control; there is no encryption-at-rest facility in the repo (stated, not implied).
- Additive migration (0056), no backfill. Old catalog images keep working — the per-System inject
  is authoritative and an old image's vestigial baked key is never presented. Systems provisioned
  before this change have no key row, so `authorize`/drgn on them fails closed
  (`CONFIGURATION_ERROR`); they can be reprovisioned.
- Recoverability is moot: nothing long-lived to back up; a lost key means reprovision.
- Remote-libvirt is out of scope here — it reuses the table/service but needs its own injection
  channel (its disk lives on a remote host); tracked as a follow-up.

## Considered & rejected

- **Host-local key file next to the overlay.** Simpler and more least-privilege, but host-local, so
  it breaks worker-agnostic or remote servicing of a later `authorize`/drgn job. DB storage is
  worker-agnostic.
- **Single shared deployment/service key** (keep baking; fix only the residence). Fixes the
  divergence with least churn but still embeds one standing, immortal, deployment-wide root
  credential in shared images — the property this change exists to remove.
- **Inject the agent's key at provision and drop kdive's own foothold.** The worker still needs
  root SSH for live drgn and console work and does not hold the agent's private key, so a
  kdive-held per-System key is required regardless.
- **Deliver the bootstrap key via the cloud-init NoCloud seed** (reuse #962). The bootstrap
  foothold must not depend on first-boot config succeeding; `--ssh-inject` writes the
  authorized_keys directly, independent of cloud-init.
