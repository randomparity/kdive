# Per-System SSH bootstrap key — design

**Issue:** #963 · **ADR:** 0289 · **Migration:** 0056 · **Supersedes:** ADR-0052 · **Revises:**
ADR-0271 (`authorize_ssh_key`), the drgn-live transport (#762/#697), and the ADR-0288 "managed
key stays on `--ssh-inject`" bullet.

## Problem

Catalog rootfs images bake a **standing root SSH key** into the base QCOW2: the family customizers
`--ssh-inject root:file:<managed pubkey>` (ADR-0052), and the worker holds the matching private
half to root-SSH a provisioned System for `authorize_ssh_key` (ADR-0271) and live drgn
introspection (`providers/local_libvirt/debug/introspect.py`, #762/#697). Two defects, surfaced by
the #962 live proof:

1. **Immortal, deployment-wide credential.** The public half is in *every* catalog image; the
   private half grants root on *every System ever provisioned from any image*, indefinitely.
   Rotating it requires re-baking the whole catalog, and the stored S3 image is itself a
   root-granting artifact.
2. **Build/worker key divergence.** `prereqs/managed_ssh_key.py` lazily generates the keypair
   per-host under XDG (`~/.local/share/kdive/ssh`). Build as one principal (`dave`) and worker as
   another (`root`) yields different keypairs, so `authorize_ssh_key` fails `Permission denied`
   (exit 255) — the exact agent path an agent would use is broken.

Root cause: the keypair is treated as host-local ambient state when it is per-System
infrastructure state.

## Decision

Replace the baked standing key with a **per-System unique bootstrap keypair** generated at
provision time and injected into the per-System overlay. No credential is baked into catalog
images.

- **Provision:** the `provision`/`reprovision` **handler** (which holds the DB connection)
  generates a throwaway ed25519 keypair unique to the System *once*, stores the private half in a
  new `system_bootstrap_keys` table, and passes the **public** half to the provider, which injects
  it into the per-System overlay *when it creates the overlay*.
- **Use:** `authorize_ssh_key` and live drgn load *this System's* private key (replacing
  `managed_private_key_path()`), root-SSH the guest over the loopback forward, and do their work.
- **Teardown:** the teardown handler deletes the System's key row (`_reclaim_*` pattern); the
  `ON DELETE CASCADE` FK is the backstop for a hard row delete.

Result: no shared or immortal credential anywhere; blast radius = one System for its lifetime; no
host-to-host key distribution; no re-bake to rotate; stored images carry no credential and are
safe to treat as public.

## Architecture

The layering is dictated by the connectionless provider seam (`LocalLibvirtProvisioning` "owns no
Postgres — the `systems.*` handlers drive the state machine"):

| Concern | Owner | Why |
| --- | --- | --- |
| Generate / store / load / reclaim the private key | `systems` job handlers (`conn`) | transactional, holds the DB connection |
| Inject the public key into the overlay | provider `provision()` | owns the overlay + libguestfs, connectionless |

### 1. Storage — `system_bootstrap_keys` table (migration 0056)

```sql
CREATE TABLE system_bootstrap_keys (
    system_id   uuid PRIMARY KEY REFERENCES systems (id) ON DELETE CASCADE,
    private_key text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

- Worker-agnostic: any worker servicing a later `authorize`/drgn job for the System loads the key
  from the DB (no host-local key file pinning it to the provisioning host — matters for >1 worker
  and for the remote follow-up).
- Registered with `SecretRegistry` on load so it is redacted from logs/errors.
- At-rest protection rests on DB access control — there is **no encryption-at-rest facility in the
  repo** (`security/secrets/secret_registry.py` is runtime redaction only). Stated, not implied.

### 2. Key service — `prereqs/system_bootstrap_key.py` (new)

Pure stdlib keygen + typed DB accessors:

- `generate_keypair() -> (private_pem: str, public_openssh: str)` — ed25519 via `ssh-keygen` to a
  `mkdtemp(0o700)` scratch, read both halves, unlink. Mirrors the mode/timeout discipline of the
  deleted `managed_ssh_key.py`.
- `async ensure_system_bootstrap_key(conn, system_id) -> str` — returns the **public** key; if no
  row exists, generate a keypair, `INSERT` the private half, return the public half; if a row
  exists, re-derive the public half from the stored private key (`ssh-keygen -y`) and return it.
  Idempotent: a provision retry reuses the stored key, never regenerates one the running disk
  would not trust.
- `async load_system_bootstrap_private_key(conn, system_id) -> str` — return the stored private
  key (register with `SecretRegistry`); raise `CONFIGURATION_ERROR` if absent.
- `async delete_system_bootstrap_key(conn, system_id) -> None` — idempotent `DELETE`.

### 3. Injection — extensible provision-time overlay-customization seam

`provision()` gains an ordered list of **overlay customizers** run against the per-System overlay
**only when it creates the overlay** (`PreparedOverlay.created`), so a retry against a running
QEMU never re-mutates a live disk:

```python
type OverlayCustomizer = Callable[[str], None]   # (overlay_path) -> None
```

- The handler builds `[inject_authorized_key(pubkey)]` and passes it in; `provision(system_id,
  profile, *, overlay_customizers=())` runs each after `prepare_overlay` iff `overlay.created`,
  before `_define_and_start`. A `provision` failure still reclaims the overlay (existing
  transactional cleanup), so a half-injected overlay is removed on retry.
- `inject_authorized_key` is a `ProvisioningFiles`-style injected seam defaulting to a real
  `virt-customize -a <overlay> --ssh-inject root:file:<pubfile>` implementation (a libguestfs pass
  on the provision hot path — one op, seconds), so unit tests drive `provision()` without
  libguestfs. Deliberately **off the cloud-init path**: the bootstrap foothold must not depend on
  first-boot config succeeding.
- **Extensibility (required):** key-injection is the *first* consumer of this seam. Future
  features that must mutate a System at provision append a customizer to the list rather than
  bolting on a parallel one-off. The list is ordered; customizers are independent.

### 4. Handler wiring

- `provision_handler` / `reprovision_handler`: before the `to_thread(provisioner.provision, …)`
  call, `pubkey = await ensure_system_bootstrap_key(conn, system_id)`, then pass
  `overlay_customizers=(inject_authorized_key(pubkey),)`. Reprovision wipes+recreates the overlay,
  so it re-injects the **same** stored key into the fresh overlay (reuse, not rotate).
- `teardown_handler`: `await delete_system_bootstrap_key(conn, system_id)` alongside the existing
  `_reclaim_console_artifacts` / `_reclaim_sysrq_artifacts`.

### 5. Re-sourcing the two SSH consumers

- `jobs/handlers/ssh_authorize.py`: `build_authorize_argv(port)` →
  `build_authorize_argv(port, key_path)`; the handler writes the loaded private key to a
  `mkdtemp(0o700)` file for the `ssh -i` argv and unlinks it after. The remote append script is
  unchanged.
- `providers/local_libvirt/debug/introspect.py`: `_live_ssh_argv` takes the per-System key path
  the same way. The provider seam is connectionless, so the private key is loaded by the
  introspect **handler** (which has `conn`) and threaded down as a path/secret, mirroring the
  existing `secret_registry` threading.

### 6. Build side — remove the baked key

- `images/families/_fedora_customize.py` / `rhel.py` / `debian.py`: remove the `--ssh-inject` of
  the managed key from `customize_argv`. New images bake **no** authorized key.
- Delete `prereqs/managed_ssh_key.py` and all call sites (build-time pubkey bake, any CLI/prereq
  that ensured the managed keypair). Enumerated in the plan.

## Migration / compatibility

- **Old catalog images keep working.** The per-System inject is authoritative; an old image's
  vestigial baked managed key is simply never presented (the worker `ssh -i`'s the per-System
  key). No forced re-bake — optional later to purge the vestigial key.
- Migration 0056 is additive (new table, FK to `systems`). No backfill: existing running Systems
  provisioned before this change have no bootstrap-key row, so `authorize`/drgn on them raises
  `CONFIGURATION_ERROR` (fail closed) — acceptable for ephemeral dev Systems; they can be
  reprovisioned.

## Failure modes

- Keygen or inject failure at provision → provision fails closed (existing `PROVISIONING_FAILURE`
  path reclaims the overlay).
- Missing key row at authorize/drgn time → `CONFIGURATION_ERROR` with `reason`.
- Teardown delete is idempotent; a crashed teardown re-run is a no-op.
- Recoverability is moot: nothing long-lived to back up — a lost key means tear down and
  reprovision.

## Scope

- **This issue:** migration 0056, the key service, the provision-time overlay-customizer seam +
  key-injection, handler wiring (provision/reprovision/teardown), re-sourcing authorize + drgn,
  removing the baked key + deleting `managed_ssh_key.py`.
- **Follow-up:** remote-libvirt reuses the table/service but needs its own injection channel
  (its overlay/disk lives on a remote host); tracked separately.

## Testing

- **Unit:** keygen shape/mode; `ensure_*` idempotency (second call returns the same public key, no
  second row); `load_*` raises on absent row; `delete_*` idempotent; provision runs customizers
  iff `overlay.created` and skips them on the reuse path; `build_authorize_argv`/`_live_ssh_argv`
  build from the per-System path; teardown handler deletes the row; families' `customize_argv` no
  longer emits `--ssh-inject`.
- **Migration:** apply/rollback 0056; `ON DELETE CASCADE` removes the key when a `systems` row is
  deleted.
- **Live e2e (operator-run, behind live-VM markers):** rebuild the two dev images keyless,
  provision a System, `systems.authorize_ssh_key` succeeds, an agent SSHes in as root and runs an
  in-guest command — the full agent path that #962's proof left blocked.

## Considered & rejected

- **Host-local key file next to the overlay** (`/var/lib/kdive/rootfs/<id>-key`). Simpler and more
  least-privilege, but host-local: it breaks any worker-agnostic or remote servicing of a later
  `authorize`/drgn job. DB storage is worker-agnostic.
- **Single shared deployment/service key** (keep baking, fix only the residence). Fixes the
  divergence with least churn, but still embeds one standing, immortal, deployment-wide root
  credential in shared images — the exact property this change removes.
- **Inject the agent's key directly at provision, drop kdive's foothold.** The worker still needs
  its own root SSH for live drgn and console work (kdive does not hold the agent's private key), so
  a kdive-held per-System key is required regardless.
- **Deliver the bootstrap key via the cloud-init NoCloud seed** (reuse #962). Rejected: the
  bootstrap foothold must not depend on first-boot config succeeding; `--ssh-inject` writes the
  authorized_keys directly and is independent of cloud-init.
