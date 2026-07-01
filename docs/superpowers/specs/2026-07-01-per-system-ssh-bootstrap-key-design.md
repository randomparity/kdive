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

- `generate_keypair() -> (private_pem: str, public_openssh: str)` — ed25519 via `ssh-keygen` into a
  `mkdtemp(0o700)` scratch, read both halves, unlink in a `finally` (guaranteed cleanup on every
  path). Mirrors the mode/timeout discipline of the deleted `managed_ssh_key.py`.
- `async ensure_system_bootstrap_key(conn, system_id) -> str` — returns the **public** key.
  Concurrency-safe by construction: `INSERT ... ON CONFLICT (system_id) DO NOTHING` with a freshly
  generated keypair, then `SELECT private_key` for the (winning or pre-existing) row and re-derive
  the public half from it (`ssh-keygen -y` on a 0600 temp file, `finally`-unlinked). Two concurrent
  provisions therefore converge on **one** row and one pubkey regardless of ordering or whether the
  caller holds the per-System lock; a retry reuses the stored key, never regenerating one the
  running disk would not trust. The freshly generated private half of a losing INSERT is simply
  discarded (never written).
- `async load_system_bootstrap_private_key(conn, system_id) -> str` — return the stored private
  key (register with `SecretRegistry`); raise `CONFIGURATION_ERROR` if absent.
- `async delete_system_bootstrap_key(conn, system_id) -> None` — idempotent `DELETE`.

**Commit-ordering invariant (critical).** `ensure_system_bootstrap_key` must run in its **own
committed transaction, before** the `conn.transaction()` block that drives `provisioner.provision`
(the provision handler wraps its whole body — including the `to_thread(provision)` call — in one
transaction under `advisory_xact_lock(SYSTEM)`; systems.py). The overlay (filesystem) and domain
(libvirt) are non-transactional side effects. If the key row were written in that same transaction
and a *later* step rolled it back after `provision` had already injected the pubkey into the
overlay, a retry would find no row, generate a new key, see the overlay already present (so skip
injection), and leave a running overlay trusting a key the DB no longer records — the exit-255
mismatch this change exists to remove. Committing the key row first makes the invariant hold:
**an overlay exists ⇒ the private half of the key it trusts is durably recorded.** The `ON CONFLICT`
upsert keeps that early commit safe under concurrent provisions.

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

- `provision_handler` / `reprovision_handler`: `pubkey = await ensure_system_bootstrap_key(conn,
  system_id)` runs **and commits before** the handler's `conn.transaction()` /
  `advisory_xact_lock` block (the commit-ordering invariant above), then the in-transaction
  `to_thread(provisioner.provision, …)` is passed `overlay_customizers=(inject_authorized_key(
  pubkey),)`. Reprovision wipes+recreates the overlay, so it re-injects the **same** stored key
  into the fresh overlay (reuse, not rotate).
- `teardown_handler`: `await delete_system_bootstrap_key(conn, system_id)` alongside the existing
  `_reclaim_console_artifacts` / `_reclaim_sysrq_artifacts`.

### 5. Re-sourcing the two SSH consumers

Both consumers materialize the loaded private key to a **0700 dir + 0600 file** (ssh refuses a
group/world-readable key) via a context manager that guarantees `unlink` on every path (a crash
between write and use must not leak a per-System private key — a window strictly shorter than
today's persistent managed-key file, but still closed explicitly).

- `jobs/handlers/ssh_authorize.py`: `build_authorize_argv(port)` →
  `build_authorize_argv(port, key_path)`; the handler loads the key
  (`load_system_bootstrap_private_key`, which it can — it already has `conn`), materializes it to
  the 0600 temp file for the `ssh -i` argv, and cleans up in a `finally`. The remote append script
  is unchanged.
- drgn-live introspection: the **connectionless provider engine** (`introspect.py`
  `_live_ssh_argv` / `introspect_live` / `run_script`) currently calls `managed_private_key_path()`
  directly. It cannot load from the DB, so the **MCP tool** `mcp/tools/debug/introspect.py` (which
  holds `conn`) loads the per-System private key, materializes it to the 0600 temp file, and passes
  the **path** into the engine method (a new parameter alongside `transport_handle`), replacing the
  engine's direct `managed_private_key_path()` call; the tool owns the temp-file lifecycle. This is
  a signature change to the engine's introspect entry points, not an in-place swap — sized
  accordingly in the plan.

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
- **Post-provision rollback:** because the key row is committed before the provision transaction
  (commit-ordering invariant), a rollback of the provision transaction after the overlay was
  injected still leaves the key row intact, so the retry reuses the matching key rather than
  minting a mismatched one.
- Missing key row at authorize/drgn time → `CONFIGURATION_ERROR` with `reason`.
- Teardown delete is idempotent; a crashed teardown re-run is a no-op. An `authorize`/drgn job
  cannot race teardown on the same System: both run under `advisory_xact_lock(SYSTEM)`.
- Recoverability is moot: nothing long-lived to back up — a lost key means tear down and
  reprovision.

## Scope

- **This issue:** migration 0056, the key service, the provision-time overlay-customizer seam +
  key-injection, handler wiring (provision/reprovision/teardown), re-sourcing authorize + drgn,
  removing the baked key + deleting `managed_ssh_key.py`.
- **Follow-up:** remote-libvirt reuses the table/service but needs its own injection channel
  (its overlay/disk lives on a remote host); tracked separately.

## Testing

- **Unit:** keygen shape/mode + guaranteed temp cleanup; `ensure_*` idempotency (second call
  returns the same public key, no second row) **and concurrency** (two `ensure_*` on one
  system_id → one row, one pubkey, via the `ON CONFLICT` upsert); `load_*` raises on absent row and
  registers the secret; `delete_*` idempotent; provision runs customizers iff `overlay.created` and
  skips them on the reuse path; the per-System key temp file is 0600 and unlinked on the error
  path; `build_authorize_argv`/the introspect engine build from the passed per-System key path;
  teardown handler deletes the row; families' `customize_argv` no longer emits `--ssh-inject`.
- **Ordering invariant:** a test that a provision-transaction rollback *after* the key row is
  committed leaves the row intact (so a retry reuses the same key), pinning the commit-before-
  overlay-creation contract.
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
